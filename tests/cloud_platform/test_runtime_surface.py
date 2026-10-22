"""Tests for the AgentCore invocation surface and the ARM64 container content.

Offline. No socket is opened, no image is built, and no container is run. The
container assertions read the `Dockerfile` and its build context as text.
REQ-FUN-38 and REQ-SEC-04 remain Pending PF6.
"""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue, ValidationError

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.runtime import (
    AGENTCORE_REQUEST_HEADERS,
    CONTAINER_BIND_HOST,
    CONTAINER_BIND_PORT,
    CONTAINER_RUN_ROOT,
    INVOCATIONS_PATH,
    MIN_INVOCATION_SECONDS,
    PING_PATH,
    RUNTIME_IDENTITY_VARIABLES,
    BindAddress,
    ControllerRuntime,
    InvocationRequest,
    UnverifiedIdentity,
    main,
    missing_runtime_identity,
)
from unstuck.cloud.session import RuntimeSessionId
from unstuck.domain import Reason, RunState, TaskSpec
from unstuck.storage import Snapshot

from .conftest import SYNTHETIC_CONTROLLER_ARN

DEPLOYMENT = Path(__file__).resolve().parents[2] / "deployment"
DOCKERFILE = DEPLOYMENT / "Dockerfile"
LOCKFILE = DEPLOYMENT / "requirements-runtime.txt"
BASE_IMAGE_LOCK = DEPLOYMENT / "base-image.lock"

HEADERS = {"x-amzn-bedrock-agentcore-runtime-session-id": "offline"}
#: A fully injected deployment environment. Every value is visibly synthetic,
#: and none of them is a credential: the entrypoint has no slot for one.
CONFIGURED = {name: "offline-synthetic" for name in RUNTIME_IDENTITY_VARIABLES}
SENSITIVE_BUILD_TOKENS = re.compile(
    r"authorization|secret|password|api[_-]?key|credential|token", re.I
)
#: Ways an arm64 build acquires emulation. Each needs a privileged container to
#: register a binfmt handler on the build host, so none is used or documented.
#: The words themselves are allowed: naming a route in order to rule it out is
#: the point of the prose, and only an actual invocation is a finding.
EMULATION_INSTALLERS = (
    "tonistiigi/binfmt",
    "multiarch/qemu-user-static",
    "qemu-user-static",
    "docker run --privileged",
    "--privileged --rm",
    "binfmt --install",
)


class AcceptingVerifier:
    """Test-only verifier. Production ships the fail-closed stub instead."""

    def __init__(self, principal: str = SYNTHETIC_CONTROLLER_ARN):
        self.principal = principal

    def verify(self, headers: dict[str, str]) -> str:
        if not headers:
            raise CloudFault(CloudReason.IDENTITY_REQUIRED)
        return self.principal


class FakeRepository:
    """Minimal in-memory stand-in for the run repository protocol."""

    def __init__(self, *, grant_lease: bool = True):
        self.grant_lease = grant_lease
        self.snapshots: dict[UUID, Snapshot] = {}

    def create_run(
        self, task: TaskSpec, dataset_version: str, idempotency_key: str
    ) -> Snapshot:
        from unstuck.cloud.dynamodb import initial_snapshot

        snapshot = initial_snapshot(task, dataset_version, idempotency_key)
        self.snapshots[snapshot.state.run_id] = snapshot
        return snapshot

    def load_run(self, run_id: UUID) -> Snapshot:
        return self.snapshots[run_id]

    def acquire(
        self, run_id: UUID, owner: UUID, *, run_seconds: float = 120.0
    ) -> Snapshot | None:
        if not self.grant_lease:
            return None
        snapshot = self.snapshots[run_id]
        state = RunState.model_validate(
            snapshot.state.model_dump()
            | {
                "status": "running",
                "lease_owner": owner,
                "lease_expires_at": 150.0,
                "lease_expires_utc": datetime(2026, 10, 1, 12, 2, 30, tzinfo=UTC),
                "lease_clock_epoch": uuid4(),
                "run_deadline_at": 120.0,
            }
        )
        return Snapshot.model_validate(snapshot.model_dump() | {"state": state})


def runtime(
    task: TaskSpec, *, grant_lease: bool = True, verifier: Any = None
) -> ControllerRuntime:
    def execute(snapshot: Snapshot, owner: UUID) -> dict[str, JsonValue]:
        assert owner is not None
        return {"status": "unresolved", "reason_code": Reason.INSUFFICIENT.value}

    return ControllerRuntime(
        FakeRepository(grant_lease=grant_lease),
        task,
        "v17",
        execute=execute,
        verifier=verifier or AcceptingVerifier(),
    )


def test_ping_reports_readiness_without_restricted_content_offline(
    task: TaskSpec,
) -> None:
    status, body = runtime(task).handle("GET", PING_PATH, {}, None)
    assert status == 200
    assert body == {"status": "ready", "mode": "offline", "provider": "mock"}


def test_invocations_accepts_a_typed_request_and_returns_a_result_offline(
    task: TaskSpec,
) -> None:
    status, body = runtime(task).handle(
        "POST",
        INVOCATIONS_PATH,
        HEADERS,
        {"idempotency_key": "offline-case-01"},
    )
    assert status == 200
    assert body["status"] == "accepted"
    assert body["mode"] == "offline"
    assert RuntimeSessionId(value=str(body["runtime_session_id"])).run_id == UUID(
        str(body["run_id"])
    )
    assert body["result"] == {
        "status": "unresolved",
        "reason_code": "insufficient_evidence",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"task": {"dataset_id": "ds-18"}},
        {"mode": "live"},
        {"dataset_version": "v18"},
        {"order_date_utc": "2026-10-02"},
    ],
)
def test_a_request_carrying_a_task_or_mode_argument_is_rejected_offline(
    task: TaskSpec, extra: dict[str, JsonValue]
) -> None:
    status, body = runtime(task).handle(
        "POST",
        INVOCATIONS_PATH,
        HEADERS,
        {"idempotency_key": "offline-case-01"} | extra,
    )
    assert status == 400
    assert body["reason_code"] == CloudReason.ARGUMENT_REJECTED.value
    assert body["status"] == "rejected"


def test_an_invocation_without_a_verified_identity_is_denied_offline(
    task: TaskSpec,
) -> None:
    status, body = runtime(task, verifier=UnverifiedIdentity()).handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "offline-case-01"}
    )
    assert status == 403
    assert body["reason_code"] == CloudReason.IDENTITY_UNVERIFIED.value
    assert "PF6" in UnverifiedIdentity.mechanism
    missing, denied = runtime(task).handle(
        "POST", INVOCATIONS_PATH, {}, {"idempotency_key": "offline-case-01"}
    )
    assert missing == 403
    assert denied["reason_code"] == CloudReason.IDENTITY_REQUIRED.value


def test_a_losing_duplicate_invocation_reports_the_run_in_flight_offline(
    task: TaskSpec,
) -> None:
    """Losing the acquisition means the work is owned, not that it is lost."""
    status, body = runtime(task, grant_lease=False).handle(
        "POST", INVOCATIONS_PATH, HEADERS, {"idempotency_key": "offline-case-01"}
    )
    assert status == 200
    assert body["status"] == "in_flight"
    assert body["reason_code"] == CloudReason.LEASE_REJECTED.value
    assert body["run_id"] is not None
    assert body["result"] is None


def test_unknown_routes_and_methods_fail_closed_offline(task: TaskSpec) -> None:
    surface = runtime(task)
    for method, path in (("POST", PING_PATH), ("GET", INVOCATIONS_PATH), ("GET", "/")):
        status, body = surface.handle(method, path, HEADERS, None)
        assert status == 404
        assert body["reason_code"] == CloudReason.ROUTE_UNKNOWN.value


def test_the_bind_address_is_the_container_boundary_only_offline(
    task: TaskSpec,
) -> None:
    bind = BindAddress()
    assert (bind.host, bind.port) == (CONTAINER_BIND_HOST, CONTAINER_BIND_PORT)
    assert (CONTAINER_BIND_HOST, CONTAINER_BIND_PORT) == ("0.0.0.0", 8080)
    assert bind.published is False
    with pytest.raises(ValidationError):
        BindAddress.model_validate({"host": "0.0.0.0", "port": 8081})
    with pytest.raises(ValidationError):
        BindAddress.model_validate({"host": "0.0.0.0", "port": 8080, "published": True})
    assert runtime(task).bind == bind


def test_the_invocation_allowance_is_at_least_the_run_budget_offline(
    task: TaskSpec,
) -> None:
    assert MIN_INVOCATION_SECONDS == 120.0
    assert runtime(task).invocation_seconds >= MIN_INVOCATION_SECONDS
    with pytest.raises(ValidationError):
        ControllerRuntime(
            FakeRepository(),
            task,
            "v17",
            execute=lambda snapshot, owner: {},
            invocation_seconds=119.0,
        )


def test_the_invocation_request_contract_is_closed_offline() -> None:
    request = InvocationRequest(idempotency_key="offline-case-01")
    assert request.idempotency_key == "offline-case-01"
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate({"idempotency_key": "x", "task": {}})
    with pytest.raises(ValidationError):
        InvocationRequest.model_validate({"idempotency_key": ""})


def test_the_container_entrypoint_refuses_to_serve_and_names_the_mechanism(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With configuration present the refusal is the identity one."""
    assert main(CONFIGURED) == 1
    report = json.loads(capsys.readouterr().err)
    assert report["serving"] == "refused"
    assert report["reason_code"] == CloudReason.IDENTITY_UNVERIFIED.value
    assert report["missing_deployment_inputs"] == []
    assert "PF6" in report["mechanism"]
    assert report["bind"] == {"host": "0.0.0.0", "port": 8080, "published": False}
    assert any("PF8" in step for step in report["checklist"])
    assert any("no host port" in step.lower() for step in report["checklist"])
    assert f'"{PING_PATH}"' not in json.dumps(report)


def test_the_entrypoint_refuses_and_names_every_missing_deployment_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Configuration is the first refusal, and it says exactly what is absent."""
    assert main({}) == 1
    report = json.loads(capsys.readouterr().err)
    assert report["serving"] == "refused"
    assert report["reason_code"] == CloudReason.CONFIG_INVALID.value
    assert report["missing_deployment_inputs"] == list(RUNTIME_IDENTITY_VARIABLES)
    assert report["forwarded_headers"] == list(AGENTCORE_REQUEST_HEADERS)
    assert any("RUNTIME_IDENTITY_VARIABLES" in step for step in report["checklist"])
    partial = dict(CONFIGURED)
    del partial["UNSTUCK_REGION"]
    partial["UNSTUCK_RUN_TABLE"] = "   "
    assert main(partial) == 1
    second = json.loads(capsys.readouterr().err)
    assert second["reason_code"] == CloudReason.CONFIG_INVALID.value
    assert second["missing_deployment_inputs"] == [
        "UNSTUCK_REGION",
        "UNSTUCK_RUN_TABLE",
    ]


def test_the_entrypoint_reads_the_process_environment_when_given_none(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real container hands nothing in, so the default is the environment."""
    for name in RUNTIME_IDENTITY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert main() == 1
    report = json.loads(capsys.readouterr().err)
    assert report["reason_code"] == CloudReason.CONFIG_INVALID.value
    assert report["missing_deployment_inputs"] == list(RUNTIME_IDENTITY_VARIABLES)


def test_no_forwarded_header_carries_a_second_credential_path() -> None:
    """SigV4 authorizes the caller before the container sees the request."""
    assert missing_runtime_identity(CONFIGURED) == ()
    assert len(AGENTCORE_REQUEST_HEADERS) == len(set(AGENTCORE_REQUEST_HEADERS))
    assert 1 <= len(AGENTCORE_REQUEST_HEADERS) <= 20
    lowered = {header.lower() for header in AGENTCORE_REQUEST_HEADERS}
    for forbidden in ("authorization", "cookie", "x-api-key", "proxy-authorization"):
        assert forbidden not in lowered


def test_the_entrypoint_module_opens_no_socket_offline() -> None:
    module = Path(inspect_source_path())
    source = module.read_text(encoding="utf-8")
    for forbidden in (
        "import socket",
        "socketserver",
        "http.server",
        "HTTPServer",
        ".listen(",
        ".bind(",
        "serve_forever",
        "uvicorn",
        "fastapi",
    ):
        assert forbidden not in source, forbidden


def inspect_source_path() -> str:
    import unstuck.cloud.runtime as surface

    assert surface.__file__ is not None
    return surface.__file__


def logical_instructions(text: str) -> list[str]:
    """The Dockerfile's instructions, with backslash continuations joined."""
    joined = text.replace("\\\n", " ")
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def build_stages(text: str) -> list[tuple[str, list[str]]]:
    """Each `FROM` line paired with the instructions that follow it."""
    stages: list[tuple[str, list[str]]] = []
    for line in logical_instructions(text):
        if line.startswith("FROM"):
            stages.append((line, []))
        elif stages:
            stages[-1][1].append(line)
    return stages


def test_the_dockerfile_cross_builds_arm64_from_a_native_builder_offline() -> None:
    """The observed failure this guards is `exec /bin/sh: exec format error`.

    An x86_64 build host without binfmt emulation cannot execute a linux/arm64
    instruction, so the first `RUN` in an arm64 stage aborts the build. The
    cross-build keeps every executed instruction on the native build platform
    and lets the arm64 stage copy the result. No emulation is installed, and
    installing one would need a privileged container this project does not run.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    stages = build_stages(text)
    assert len(stages) == 2, "one native builder stage and one arm64 final stage"
    builder, final = stages
    assert "--platform=$BUILDPLATFORM" in builder[0]
    assert "linux/arm64" not in builder[0]
    assert "AS builder" in builder[0]
    assert "--platform=linux/arm64" in final[0]
    assert "$BUILDPLATFORM" not in final[0]


def test_the_arm64_stage_executes_no_build_instruction_offline() -> None:
    """Without emulation the arm64 stage may only copy bytes and declare."""
    _, final = build_stages(DOCKERFILE.read_text(encoding="utf-8"))
    executed = [line for line in final[1] if line.split()[0] in {"RUN", "SHELL"}]
    assert executed == [], executed


def test_both_base_images_are_digest_pinned_build_inputs_offline() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    builder, final = build_stages(text)
    assert "@${BUILDER_IMAGE_DIGEST}" in builder[0]
    assert "@${RUNTIME_IMAGE_DIGEST}" in final[0]
    for line in (builder[0], final[0]):
        assert ":latest" not in line
        # A tag after the digest would reintroduce a mutable reference.
        assert not re.search(r"@\$\{\w+\}:", line), line
    for declared in ("ARG BUILDER_IMAGE_DIGEST", "ARG RUNTIME_IMAGE_DIGEST"):
        assert declared in text
        # No default value: a build without the digest fails rather than
        # resolving a floating tag.
        assert f"{declared}=" not in text
    assert BASE_IMAGE_LOCK.exists()
    lock = BASE_IMAGE_LOCK.read_text(encoding="utf-8")
    assert "BUILDER_IMAGE_DIGEST=" in lock
    assert "RUNTIME_IMAGE_DIGEST=" in lock
    recorded = dict(
        line.strip().split("=", 1)
        for line in lock.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    # This file used to be required to be empty, on the grounds that any digest
    # recorded here would be a fabricated fact. That held only while no registry
    # read had happened. Once an operator resolves the digests from a real
    # `docker buildx imagetools inspect` and keeps the manifest as evidence, a
    # recorded digest is a verified fact, and requiring emptiness would forbid
    # recording it. What must never happen is a MUTABLE reference, so that is
    # what is asserted now: every recorded value is an immutable sha256 digest,
    # and no tag may be recorded at all.
    for name, value in recorded.items():
        assert name in {"BUILDER_IMAGE_DIGEST", "RUNTIME_IMAGE_DIGEST"}, name
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", value), (name, value)
    assert ":latest" not in lock
    assert not re.search(r"^\s*[A-Z_]+_TAG=", lock, re.MULTILINE), (
        "a tag is mutable; only digests may be recorded here"
    )


def test_the_builder_stages_arm64_wheels_without_running_them_offline() -> None:
    """Cross-installing resolves wheels by tag; it never executes one."""
    builder, _ = build_stages(DOCKERFILE.read_text(encoding="utf-8"))
    pip = next(line for line in builder[1] if "pip install" in line)
    assert "--only-binary=:all:" in pip, "no wheel may be built from source here"
    assert "--no-deps" in pip, "the lockfile stays the only authority"
    assert "--implementation cp" in pip
    assert "--python-version" in pip
    assert "--abi" in pip
    assert "--target" in pip, "a cross-platform install needs an explicit target"
    assert "--no-compile" in pip, "the native interpreter byte-compiles nothing"
    platforms = re.findall(r"--platform (\S+)", pip)
    assert platforms, "a cross-platform install needs an explicit platform tag"
    for tag in platforms:
        assert tag.endswith("_aarch64"), tag
        assert tag.startswith("manylinux"), tag
    assert "manylinux_2_17_aarch64" in platforms
    for line in (entry for entry in builder[1] if entry.startswith("RUN")):
        for executes_a_wheel in ("python -c", "-m unstuck", "-m uvicorn", "import "):
            assert executes_a_wheel not in line, line
    assert not any(line.startswith("ENV PYTHONPATH") for line in builder[1]), (
        "the builder must not place the staged arm64 packages on its own path"
    )


def test_the_run_directory_is_staged_and_copied_already_owned_offline() -> None:
    """The arm64 stage cannot chown, so ownership arrives with the COPY."""
    builder, final = build_stages(DOCKERFILE.read_text(encoding="utf-8"))
    staged = next(line for line in builder[1] if "mkdir" in line)
    assert staged.startswith("RUN")
    assert CONTAINER_RUN_ROOT.name in staged
    owned = next(line for line in final[1] if "--chown=10001:10001" in line)
    assert owned.startswith("COPY ")
    assert "--from=builder" in owned
    assert owned.endswith(str(CONTAINER_RUN_ROOT.parent))


def test_the_container_user_is_numeric_and_needs_no_passwd_entry_offline() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    users = [line.split()[1] for line in text.splitlines() if line.startswith("USER ")]
    assert users == ["10001:10001"]
    assert "useradd" not in text, "an unexecuted stage adds no user"


def test_the_runtime_path_carries_the_sources_and_staged_packages_offline() -> None:
    _, final = build_stages(DOCKERFILE.read_text(encoding="utf-8"))
    path = next(line for line in final[1] if line.startswith("ENV PYTHONPATH="))
    entries = path.split("=", 1)[1].split(":")
    assert entries[0] == "/opt/unstuck/src"
    assert len(entries) == 2, "the sources and the staged packages, nothing else"
    copied = [line for line in final[1] if line.startswith("COPY")]
    assert any(
        line.endswith(entries[1]) and "--from=builder" in line for line in copied
    ), entries[1]


def test_the_documented_build_command_needs_no_emulation_offline() -> None:
    readme = (DEPLOYMENT / "README.md").read_text(encoding="utf-8")
    command = readme[readme.index("docker buildx build") :]
    assert "--build-arg BUILDER_IMAGE_DIGEST=" in command
    assert "--build-arg RUNTIME_IMAGE_DIGEST=" in command
    assert "--platform linux/arm64" in command
    lowered = (readme + DOCKERFILE.read_text(encoding="utf-8")).lower()
    for installer in EMULATION_INSTALLERS:
        assert installer not in lowered, installer


def test_the_container_runs_as_a_non_root_user_on_the_private_port_offline() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    users = [line.split()[1] for line in text.splitlines() if line.startswith("USER ")]
    assert users, "the container needs an explicit user"
    assert users[-1] not in {"root", "0", "0:0"}
    assert f"EXPOSE {CONTAINER_BIND_PORT}" in text
    for published in DEPLOYMENT.rglob("*"):
        # Source and documentation only. Compiled bytecode is not a build
        # input and is not text, so reading it would fail on its own bytes
        # rather than on anything this sweep is looking for.
        if published.is_file() and "__pycache__" not in published.parts:
            body = published.read_text(encoding="utf-8")
            assert "--publish" not in body
            assert f"-p {CONTAINER_BIND_PORT}:" not in body


def test_the_container_declares_no_secret_build_input_offline() -> None:
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(("ARG ", "ENV ")):
            assert not SENSITIVE_BUILD_TOKENS.search(stripped), stripped
        assert "--mount=type=secret" not in stripped


def test_the_container_copies_no_evaluator_or_test_asset_offline() -> None:
    copied = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith(("COPY", "ADD"))
    ]
    assert copied
    for line in copied:
        assert not line.startswith("ADD"), "ADD can fetch a remote asset"
        for forbidden in (
            "tests",
            "fixtures/evaluator",
            "experiments",
            "models",
            "aidlc",
            ".git",
            " . ",
        ):
            assert forbidden not in line, (forbidden, line)


def test_the_runtime_lockfile_is_fully_pinned_offline() -> None:
    lines = [
        line.strip()
        for line in LOCKFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[A-Za-z0-9_.\-+]+", line), line
    assert "--no-deps" in DOCKERFILE.read_text(encoding="utf-8")
