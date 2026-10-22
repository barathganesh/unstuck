"""The AgentCore custom-runtime server. FastAPI on Uvicorn, ARM64, port 8080.

This is the serving layer of the container `deployment/Dockerfile` builds. It
implements the verified AgentCore Runtime HTTP contract for a custom agent, one
built without the AgentCore Python SDK:

* `POST /invocations` and `GET /ping` are the two required paths.
* `/ping` answers `{"status": "Healthy"}`; `Healthy` and `HealthyBusy` are the
  only status values the contract defines, and `time_of_last_update` is omitted
  so the platform tracks status changes itself rather than seeing a timestamp
  advance on every probe and keeping sessions alive to `MaxLifetime`.
* The container binds `0.0.0.0:8080` and the image is `linux/arm64`.

Source: the AgentCore Runtime service contract and its HTTP protocol page,
https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html
and the `runtime-http-protocol-contract` page it links to.

Three properties are structural rather than configured.

The security boundary is layered and nothing here is the gate. Callers are
authorized by SigV4 on `InvokeAgentRuntime` at the AgentCore service boundary,
before a request reaches this process, and exactly one role holds that action.
This app adds `SessionHeaderIdentity` on top: the service-forwarded session
identity must be present and well formed. It reads no `Authorization` header,
no API key, and no cookie, and there is no shared secret to present, so a
caller cannot supply a credential this app would honour. PF6 and PF8 both stay
Pending: no live principal and no anonymous denial has been observed.

The task is server-owned. `/invocations` accepts an idempotency key and nothing
else, so a caller cannot restate the task, the dataset, the date, the mode, or
the provider. An extra field is a refusal rather than an ignored key, and the
refusal is reason-coded. Which declared synthetic scenario this container
serves is server-owned for the same reason: it arrives as the `UNSTUCK_SCENARIO`
deployment input, is resolved against the shipped allowlist before a listener
exists, and is unreachable from the request.

Telemetry is attached here and nowhere else in the cloud unit. Each durable
event the repository already accepted is projected onto its public form and
written as one line to this process's stdout, which is the log the platform
collects. The sink answers the run nothing, so no selection, argument, repair,
or disposition can depend on it, and a sink that refuses a line is counted by
the recorder rather than reaching the run.

Everything fails closed. Missing or invalid configuration refuses at
construction, before a listener exists. Every unknown path and method answers
`route_unknown`. No interactive documentation, schema, or CORS surface is
served at all, so there is nothing to disable. An unexpected exception becomes
a reason-coded body, never a stack trace on the wire.

This deployment runs the deterministic mock provider, so every response and
every readiness probe says so: `classification` is `synthetic`, `provider` is
`mock`, and `live_decider` is `Pending`. Nothing served from here can satisfy
PF1 or PF2 or support a benchmark claim.

This is deliberately the one module of the cloud unit that sits outside the
"no module imports anything that can open a socket" guard of
`tests/cloud_platform/test_integration.py`. It is the serving layer, so a
server library and an event loop are its whole purpose; every other cloud
module still imports neither, and `unstuck.cloud.runtime` beside it holds the
contracts and opens nothing.
"""

import asyncio
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from pydantic import JsonValue

from unstuck.cloud import CloudFault, CloudReason
from unstuck.cloud.narration import NarrationResult
from unstuck.cloud.runtime import (
    CONTAINER_RUN_ROOT,
    INVOCATIONS_PATH,
    MIN_INVOCATION_SECONDS,
    MOCK_MODEL_REVISION,
    PING_PATH,
    ControllerRuntime,
    IdentityVerifier,
    InvocationResponse,
    ProviderMode,
    RealProviderSettings,
    RuntimeSettings,
    SessionHeaderIdentity,
    provider_mode,
    real_provider_settings,
    runtime_settings,
)
from unstuck.domain import RuntimeFault, Settings, TaskSpec
from unstuck.operator import OperatorFault
from unstuck.storage import Snapshot

if TYPE_CHECKING:
    from unstuck.cloud.decider import SageMakerRuntime
    from unstuck.cloud.narration import BedrockNarrator
    from unstuck.operator.scenarios import Scenario

#: The contract's "ready to accept new work" status value, spelled exactly.
#: `HealthyBusy` is the other defined value and is deliberately never returned:
#: this runtime executes an invocation within its own request rather than
#: handing work to a background task, so there is no busy state to report and
#: no reason to hold a session open past its idle timeout.
HEALTHY: Final[Literal["Healthy"]] = "Healthy"

#: The only media type `/invocations` accepts, per the contract's request
#: format. Anything else is refused rather than sniffed.
JSON_MEDIA_TYPE: Final = "application/json"

#: The largest invocation body this app will read. The only field a caller may
#: send is a bounded idempotency key, so anything approaching this size is
#: already malformed, and refusing on the declared length means an oversized
#: body is never parsed.
MAX_INVOCATION_BYTES: Final = 65_536

#: What a mock deployment's responses say about what produced them. These
#: labels are applied last, so no response can report a live provider or an
#: unlabelled classification even if a lower layer forgot to.
#:
#: This remains the `mock` set exactly as it was, so an existing deployment
#: that declares no provider mode is labelled precisely as before.
RESPONSE_LABELS: Mapping[str, str] = {
    "classification": "synthetic",
    "mode": "offline",
    "provider": "mock",
    "live_decider": "Pending",
}

#: What a `real` deployment's responses say. The two providers are named
#: separately on purpose: the Decider is what selects, and the narrator is a
#: bounded read of a result that is already settled, so collapsing them into
#: one provider label would suggest narration took part in a decision.
#:
#: `classification` stays `synthetic` and `live_decider` stays `Pending` even
#: here. The fixture the controller runs against is a declared synthetic one,
#: and no provider boundary event has been observed from a deployment, so
#: neither fact changes because a hosted endpoint was configured.
REAL_RESPONSE_LABELS: Mapping[str, str] = {
    "classification": "synthetic",
    "mode": "offline",
    "provider": "decider",
    "narration_provider": "bedrock",
    "live_decider": "Pending",
}


def response_labels(
    mode: ProviderMode, *, narration: NarrationResult | None = None
) -> dict[str, JsonValue]:
    """The labels one response carries, for the mode this deployment serves.

    Narration is reported as a separate object rather than mixed into the
    result, and its absence is stated rather than omitted: `not_requested`
    means narration was never asked for, and `unavailable` means it was asked
    for and did not work, which are different facts about the same
    deterministic answer.
    """
    if mode == "mock":
        return dict(RESPONSE_LABELS)
    labels: dict[str, JsonValue] = dict(REAL_RESPONSE_LABELS)
    if narration is None:
        labels["narration_status"] = "not_requested"
        labels["narration"] = None
        return labels
    labels["narration_status"] = narration.status
    labels["narration"] = narration.model_dump(mode="json", exclude={"schema_version"})
    return labels


#: The methods the catch-all answers. Every one of them refuses, so a method
#: this app does not implement cannot read as "allowed but unimplemented".
UNKNOWN_ROUTE_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

#: The deployment input that selects which declared synthetic scenario this
#: container serves. It is deliberately not part of `RuntimeSettings` and not a
#: `RUNTIME_IDENTITY_VARIABLES` entry: those name the owner's resources, and a
#: default for one of them would be a guess about somebody else's account. This
#: names one entry in an allowlist the image itself carries, so a default is a
#: choice among values that are all present and all synthetic.
SCENARIO_VARIABLE: Final = "UNSTUCK_SCENARIO"

#: The scenario a deployment that declares none serves. It is the one the first
#: hosted runtime served, so an existing deployment that adds no variable keeps
#: the path it already had.
DEFAULT_SCENARIO: Final = "repairable-join"


def runtime_scenario(environ: Mapping[str, str] | None = None) -> "Scenario":
    """Resolve the server-owned scenario from the injected deployment inputs.

    The token names one entry in the declared allowlist the image carries. An
    undeclared token refuses here, at construction, so a mistyped deployment
    input never reaches a listener, and an empty value counts as unsupplied
    rather than as a scenario nobody declared.
    """
    from unstuck.operator.scenarios import resolve_scenario

    supplied = (environ or {}).get(SCENARIO_VARIABLE, "").strip()
    try:
        return resolve_scenario(supplied or DEFAULT_SCENARIO)
    except OperatorFault as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error


def build_controller_runtime(
    settings: RuntimeSettings,
    *,
    run_root: Path | None = None,
    verifier: IdentityVerifier | None = None,
    invocation_seconds: float = MIN_INVOCATION_SECONDS,
    scenario: str | None = None,
) -> ControllerRuntime:
    """Construct the offline synthetic controller surface from validated inputs.

    This is deliberately the same offline path the local skeleton runs: a
    declared synthetic fixture, the real controller and Decision Receipt
    engine, and the deterministic scripted stand-in for the Decider. No live
    provider is called, no AWS client is constructed, and no credential is
    read, so `classification` stays `synthetic`, `provider` stays `mock`, and
    the live Decider stays Pending. Nothing produced through here can satisfy
    PF1 or PF2 or support a benchmark claim.

    The scenario is the server's, and `scenario` is a deployment input rather
    than anything a caller can reach: the request contract still carries an
    idempotency key and nothing else. Which scenario is served decides which
    synthetic upstream state the run meets, so the real controller reaches a
    different terminal disposition through a different path for each one. It
    decides no outcome, selects no tool, and authorizes no repair.

    Two coherence checks refuse rather than mislabel. A deployment declaring a
    dataset version the image does not carry has no dataset to serve, and a
    deployment declaring any model revision other than the stand-in's would
    present a scripted answer under somebody's real checkpoint name.

    Run state lives in the container-local repository under `run_root`, so it
    is per-container and ephemeral. The durable DynamoDB run store is a
    separate step behind its own gates; no claim is made here that it is in
    use. The receipt store is per-container for the same reason, and that is
    what makes the cross-call refusal observable: the history a new decision is
    offered is the history this container actually wrote.

    Telemetry is attached, not woven in. The sink receives a projection of each
    durable event the repository already accepted and answers the run nothing,
    so no selection, argument, repair, or disposition can depend on it. It is
    the public projection that is written, to the container's own stdout, which
    is the log the platform already collects.
    """
    from unstuck.cascade.receipts import ReceiptNamespace
    from unstuck.cascade.repository import LocalReceiptRepository
    from unstuck.clock import SystemClock
    from unstuck.controller import Controller
    from unstuck.operator.deciders import OfflineScenarioDecider
    from unstuck.operator.scenarios import LOCAL_ARM, preobserved_evidence
    from unstuck.query import EvidenceEngine
    from unstuck.storage import LocalRunRepository
    from unstuck.telemetry.collector import PublicLineSink
    from unstuck.telemetry.contracts import (
        CorrelationContext,
        InfrastructureDimensions,
    )

    selected = runtime_scenario({SCENARIO_VARIABLE: scenario or ""})
    fixture = selected.fixture()
    if fixture.dataset_version != settings.dataset_version:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if settings.model_revision != MOCK_MODEL_REVISION:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    clock = SystemClock()
    try:
        repository = LocalRunRepository(run_root or CONTAINER_RUN_ROOT, clock=clock)
    except RuntimeFault as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error
    receipts = LocalReceiptRepository()
    namespace = ReceiptNamespace(arm=LOCAL_ARM, case_id=selected.case_id)
    # The hosting identity is supplied by the validated deployment inputs, so a
    # record can be grouped by deployment. The public projection collapses all
    # of it into one hash, so none of these values is ever written.
    hosting = InfrastructureDimensions(
        source="runtime_configuration",
        account_id=settings.account_id,
        region=settings.region,
        runtime_id=settings.runtime_name,
        endpoint_id=settings.endpoint_name,
    )
    sink = PublicLineSink()

    def execute(snapshot: Snapshot, owner: UUID) -> dict[str, JsonValue]:
        """Continue the run this layer already leased. Never acquire again.

        The controller's run loop is a coroutine and this callable is
        synchronous, so it drives that coroutine on a loop of its own. The
        request handler calls it from a worker thread, so this loop never
        contends with the server's own.
        """
        engine = EvidenceEngine(fixture, clock=clock)
        try:
            controller = Controller(
                repository,
                engine,
                OfflineScenarioDecider(),
                clock=clock,
                settings=Settings(baseline_id=LOCAL_ARM),
                initial_evidence=preobserved_evidence(selected, fixture, clock=clock),
                receipts=receipts,
                namespace=namespace,
                telemetry=sink,
                correlation=CorrelationContext(arm=LOCAL_ARM, case_id=selected.case_id),
                infrastructure=hosting,
            )
            return asyncio.run(controller.resume(snapshot, owner))
        finally:
            engine.close()

    return ControllerRuntime(
        repository,
        TaskSpec(dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc),
        fixture.dataset_version,
        execute=execute,
        verifier=verifier or SessionHeaderIdentity(settings),
        invocation_seconds=invocation_seconds,
    )


def build_real_controller_runtime(
    settings: RuntimeSettings,
    real: RealProviderSettings,
    *,
    decider_client: "SageMakerRuntime",
    run_root: Path | None = None,
    verifier: IdentityVerifier | None = None,
    invocation_seconds: float = MIN_INVOCATION_SECONDS,
    scenario: str | None = None,
) -> ControllerRuntime:
    """The same controller surface, with the hosted Decider answering instead.

    One thing changes and one thing only: who answers a selector call. The
    controller, the Decision Receipt engine, the verifier, the deterministic
    outcome rules, the two-call and two-read bounds, the receipt admission and
    matching rules, and the repair authority are all the shipped ones, so the
    hosted path is the local path with a different provider behind the same
    seam. Nothing here selects a tool, writes a receipt, or labels an outcome.
    The read bound may be lowered to one by the validated deployment input
    `real.max_diagnostic_reads`, never raised, and never by a caller.

    The upstream state is still a declared synthetic fixture, because that is
    what the image carries. `classification` therefore stays `synthetic` here
    too: a real provider answering about synthetic evidence is a real provider
    call and not a live measurement, and PF7 stays Pending either way.

    Two coherence checks refuse rather than mislabel. A deployment declaring a
    dataset version the image does not carry has no dataset to serve, and a
    deployment declaring the stand-in's revision has already been refused by
    `real_provider_settings`, so the revision reaching the endpoint is the
    checkpoint identity every answer is checked against.

    The receipt store is handed to the cascade rather than to the controller,
    because the hosted composition supplies its own engine. It is still
    per-container and still starts empty, which is what makes the cross-call
    refusal observable: the history a new decision is offered is the history
    this container actually wrote.
    """
    from unstuck.cascade.cards import capability_cards
    from unstuck.cascade.engine import CascadeEngine
    from unstuck.cascade.matching import HistoryMatcher
    from unstuck.cascade.policy import Calibration, CascadePolicy
    from unstuck.cascade.receipts import ReceiptNamespace
    from unstuck.cascade.repository import LocalReceiptRepository
    from unstuck.cascade.verifier import ReceiptVerifier
    from unstuck.clock import SystemClock
    from unstuck.cloud.decider import CloudDeciderProvider
    from unstuck.controller import Controller
    from unstuck.decision import default_catalog
    from unstuck.operator.scenarios import LOCAL_ARM, preobserved_evidence
    from unstuck.query import EvidenceEngine
    from unstuck.storage import LocalRunRepository
    from unstuck.telemetry.collector import PublicLineSink
    from unstuck.telemetry.contracts import CorrelationContext, InfrastructureDimensions

    selected = runtime_scenario({SCENARIO_VARIABLE: scenario or ""})
    fixture = selected.fixture()
    if fixture.dataset_version != settings.dataset_version:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if settings.model_revision != real.decider.model_revision:
        raise CloudFault(CloudReason.CONFIG_INVALID)
    clock = SystemClock()
    try:
        repository = LocalRunRepository(run_root or CONTAINER_RUN_ROOT, clock=clock)
    except RuntimeFault as error:
        raise CloudFault(CloudReason.CONFIG_INVALID) from error
    catalog = default_catalog()
    cards = capability_cards(catalog)
    namespace = ReceiptNamespace(arm=LOCAL_ARM, case_id=selected.case_id)
    hosting = InfrastructureDimensions(
        source="runtime_configuration",
        account_id=settings.account_id,
        region=settings.region,
        runtime_id=settings.runtime_name,
        endpoint_id=settings.endpoint_name,
    )
    sink = PublicLineSink()
    core = Settings(
        baseline_id=LOCAL_ARM,
        model_revision=real.decider.model_revision,
        provider_seconds=real.decider.provider_seconds,
    )
    # One store per container, constructed here rather than inside `execute`,
    # for the reason this function's docstring already gives: the history a new
    # decision is offered has to be the history this container actually wrote.
    # Constructing it per invocation would hand every call an empty corpus, so
    # `history_status` could never reach `matched` and the exact-repetition
    # refusal would be unreachable across calls. The mock composition above
    # shares its store the same way.
    store = LocalReceiptRepository()

    def execute(snapshot: Snapshot, owner: UUID) -> dict[str, JsonValue]:
        """Continue the run this layer already leased. Never acquire again."""
        cascade = CascadeEngine(
            decider=CloudDeciderProvider(decider_client, real.decider, clock=clock),
            repository=store,
            verifier=ReceiptVerifier(clock, cards=cards),
            clock=clock,
            policy=CascadePolicy(
                baseline_id=LOCAL_ARM,
                model_revision=real.decider.model_revision,
                calibration=Calibration(
                    acceptance_tier=real.acceptance_tier,
                    first_confidence_min=core.first_confidence_min,
                    final_confidence_min=core.final_confidence_min,
                ),
            ),
            cards=cards,
            matcher=HistoryMatcher(store),
            provider_seconds=real.decider.provider_seconds,
            max_diagnostic_reads=real.max_diagnostic_reads,
        )
        engine = EvidenceEngine(fixture, clock=clock)
        try:
            controller = Controller(
                repository,
                engine,
                clock=clock,
                catalog=catalog,
                settings=core,
                initial_evidence=preobserved_evidence(selected, fixture, clock=clock),
                cascade=cascade,
                namespace=namespace,
                # Identical to the controller's own fallback for this field, so
                # the cascade identity is the leased run's and nothing else.
                idempotency_key=str(snapshot.state.run_id),
                telemetry=sink,
                correlation=CorrelationContext(arm=LOCAL_ARM, case_id=selected.case_id),
                infrastructure=hosting,
                # The provider identity is not passed in. The controller reads
                # it from the normalized selection the Decider seam returned,
                # so a real checkpoint answering about a declared synthetic
                # fixture labels the run `decider` because it answered, not
                # because this deployment declared `real`. `classification`
                # stays `synthetic` either way.
            )
            return asyncio.run(controller.resume(snapshot, owner))
        finally:
            engine.close()

    return ControllerRuntime(
        repository,
        TaskSpec(dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc),
        fixture.dataset_version,
        execute=execute,
        verifier=verifier or SessionHeaderIdentity(settings),
        invocation_seconds=invocation_seconds,
    )


def _refusal(
    status: int, reason: CloudReason, mode: ProviderMode = "mock"
) -> JSONResponse:
    """One refusal vocabulary, identical to the controller's own."""
    body = InvocationResponse(status="rejected", reason_code=reason).model_dump(
        mode="json", exclude={"schema_version"}
    )
    return JSONResponse(status_code=status, content=body | response_labels(mode))


def _declared_length(request: Request) -> int | None:
    declared = request.headers.get("content-length")
    if declared is None or not declared.strip().isdigit():
        return None
    return int(declared.strip())


def narrate_result(
    narrator: "BedrockNarrator", body: Mapping[str, Any]
) -> NarrationResult:
    """Narrate one answered invocation, after the controller already finished.

    This is the whole of narration's position in the system. It runs on the
    body the runtime already returned, so the controller, the verifier and the
    deterministic outcome rules have all completed and the answer is fixed
    before a model is asked anything. It cannot run at all on a rejected,
    unresolved, or unverified result: `terminal_projection` refuses those, and
    the refusal is reported rather than retried.
    """
    from unstuck.cloud.narration import not_permitted, terminal_projection

    result = body.get("result")
    try:
        projection = terminal_projection(
            result if isinstance(result, Mapping) else None
        )
    except CloudFault:
        return not_permitted(narrator.settings.model_id)
    return narrator.narrate(projection)


def create_app(
    *,
    runtime: ControllerRuntime,
    settings: RuntimeSettings,
    mode: ProviderMode = "mock",
    narrator: "BedrockNarrator | None" = None,
) -> FastAPI:
    """Build the server over one already-constructed controller surface.

    `settings` is not a second source of configuration: it is the one the
    runtime's verifier was bound to, and a mismatch refuses. Passing a runtime
    verified against another deployment's inputs would serve one topology under
    another's identity, so the two are checked against each other here rather
    than trusted to agree.

    `mode` decides only what the response labels say, and the narrator is
    attached only in `real` mode. A narrator handed to a `mock` deployment is a
    refusal rather than an ignored argument: labelling matters here, and a mock
    run narrated by a real model would produce a response whose provider labels
    contradicted each other.
    """
    if (
        isinstance(runtime.verifier, SessionHeaderIdentity)
        and runtime.verifier.settings != settings
    ):
        raise CloudFault(CloudReason.CONFIG_INVALID)
    if mode == "mock" and narrator is not None:
        raise CloudFault(CloudReason.CONFIG_INVALID)

    app = FastAPI(
        title="Unstuck controller runtime",
        description=(
            "Private AgentCore Runtime container surface. Offline synthetic "
            "evidence; the live Decider is Pending."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exception: Exception) -> Response:
        """A bug is a refusal, never a traceback or an exception message."""
        return _refusal(500, CloudReason.CONFIG_INVALID, mode)

    @app.get(PING_PATH)
    async def ping() -> Response:
        """Readiness only. No topology value, no path, and no identity."""
        readiness = runtime.ping().model_dump(mode="json", exclude={"schema_version"})
        body: dict[str, Any] = {
            "status": HEALTHY,
            "readiness": readiness["status"],
            **response_labels(mode),
        }
        return JSONResponse(status_code=200, content=body)

    @app.post(INVOCATIONS_PATH)
    async def invocations(request: Request) -> Response:
        media = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if media != JSON_MEDIA_TYPE:
            return _refusal(415, CloudReason.ARGUMENT_REJECTED, mode)
        declared = _declared_length(request)
        if declared is not None and declared > MAX_INVOCATION_BYTES:
            return _refusal(413, CloudReason.ARGUMENT_REJECTED, mode)
        raw = await request.body()
        if len(raw) > MAX_INVOCATION_BYTES:
            return _refusal(413, CloudReason.ARGUMENT_REJECTED, mode)
        try:
            parsed = json.loads(raw)
        except ValueError:
            return _refusal(400, CloudReason.ARGUMENT_REJECTED, mode)
        if not isinstance(parsed, dict):
            # A list, string, number, or null carries no idempotency key, and
            # the closed request contract has no shape that would accept one.
            return _refusal(400, CloudReason.ARGUMENT_REJECTED, mode)
        # `handle` owns verification, admission, leasing and the run loop, and
        # the run loop is synchronous, so it runs off the event loop rather
        # than blocking every other request for the length of one run.
        status, body = await run_in_threadpool(
            runtime.handle,
            "POST",
            INVOCATIONS_PATH,
            dict(request.headers),
            parsed,
        )
        # Narration happens here and nowhere earlier: the controller and the
        # verifier have already finished and the answer above is fixed, so no
        # selection, evidence, repair, or disposition can depend on it. It runs
        # off the event loop for the same reason the run loop does.
        narration = (
            await run_in_threadpool(narrate_result, narrator, body)
            if narrator is not None
            else None
        )
        return JSONResponse(
            status_code=status,
            content=body | response_labels(mode, narration=narration),
        )

    @app.api_route("/{unmatched:path}", methods=UNKNOWN_ROUTE_METHODS)
    async def unknown(unmatched: str) -> Response:
        """Every other path and method, including the absent docs surface."""
        return _refusal(404, CloudReason.ROUTE_UNKNOWN, mode)

    return app


def bounded_client(service: str, *, region: str, budget_seconds: float) -> Any:
    """Build one AWS client whose own timeout bounds the caller's budget.

    This is the only place in the cloud unit that constructs a client, and it
    is here rather than in an adapter for two reasons. The adapters take an
    injected client so they stay testable without a credential chain and
    without a socket, and this module is the serving layer, so it is the one
    place where reaching the outside world is the whole purpose.
    No credential is read here either: the SDK resolves the AgentCore execution
    role's own SigV4 identity, and no key, profile, or endpoint override is
    passed.

    The timeout is not advisory. A synchronous SDK call cannot be preempted by
    the caller's deadline, so the per-attempt read timeout times the attempt
    count has to fit inside the budget, and both adapters refuse a client where
    it does not. Retries are off for the same reason: one attempt, measured,
    and a failure is a typed refusal rather than a second call.
    """
    import boto3
    from botocore.config import Config

    return boto3.session.Session().client(
        service,
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=budget_seconds,
            retries={"mode": "standard", "max_attempts": 0},
        ),
    )


def app_from_environment(
    environ: Mapping[str, str] | None = None, *, run_root: Path | None = None
) -> FastAPI:
    """The application factory the container command names.

    Construction is where this fails closed, in `mock` mode and in `real` mode
    alike. The injected deployment inputs are validated first, the provider
    mode is resolved second, and the controller surface is built third, so a
    container missing a variable, declaring an unknown mode, or declaring
    `real` without a Decider endpoint and a narration model raises
    `cloud_config_invalid` before Uvicorn binds anything, and the process exits
    non-zero without ever having listened.

    `mock` is the default and its path is byte for byte the one it was: an
    existing deployment that adds no variable gets the same deterministic
    stand-in, the same `provider: mock` labels, and no AWS client at all.
    """
    supplied = os.environ if environ is None else environ
    settings = runtime_settings(supplied)
    mode = provider_mode(supplied)
    scenario = supplied.get(SCENARIO_VARIABLE)
    if mode == "mock":
        return create_app(
            runtime=build_controller_runtime(
                settings, run_root=run_root, scenario=scenario
            ),
            settings=settings,
        )
    from unstuck.cloud.narration import BedrockNarrator

    real = real_provider_settings(supplied, model_revision=settings.model_revision)
    decider_client = bounded_client(
        "sagemaker-runtime",
        region=settings.region,
        budget_seconds=real.decider.provider_seconds,
    )
    narrator = BedrockNarrator(
        bounded_client(
            "bedrock-runtime",
            region=settings.region,
            budget_seconds=real.narration.timeout_seconds,
        ),
        real.narration,
    )
    return create_app(
        runtime=build_real_controller_runtime(
            settings,
            real,
            decider_client=decider_client,
            run_root=run_root,
            scenario=scenario,
        ),
        settings=settings,
        mode=mode,
        narrator=narrator,
    )


__all__ = [
    "DEFAULT_SCENARIO",
    "HEALTHY",
    "JSON_MEDIA_TYPE",
    "MAX_INVOCATION_BYTES",
    "REAL_RESPONSE_LABELS",
    "RESPONSE_LABELS",
    "SCENARIO_VARIABLE",
    "UNKNOWN_ROUTE_METHODS",
    "app_from_environment",
    "bounded_client",
    "build_controller_runtime",
    "build_real_controller_runtime",
    "create_app",
    "narrate_result",
    "response_labels",
    "runtime_scenario",
]
