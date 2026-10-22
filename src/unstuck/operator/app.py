"""The loopback-only FastAPI application.

Every request passes the `docs/spec/design.md` section 13 guards in one
middleware, so a route cannot be added that forgets them. No CORS middleware is
installed: a browser on another origin has no business here, so there is
nothing to negotiate.

The application starts and reads runs through the repository and controller
that already exist. It writes no run state itself, derives no outcome, selects
no tool, and authorizes no repair.
"""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from unstuck.cascade.receipts import ReceiptNamespace
from unstuck.cascade.repository import LocalReceiptRepository, ReceiptRepository
from unstuck.clock import Clock, SystemClock
from unstuck.domain import EvidenceRecord, Reason, RuntimeFault, Settings, TaskSpec
from unstuck.events import Redactor
from unstuck.fixtures import Fixture
from unstuck.operator import OperatorFault, OperatorReason
from unstuck.operator.benchmark import EXPORT_FILENAME, benchmark_view, load_export
from unstuck.operator.contracts import BindAddress, ExportRequest, RunRequest
from unstuck.operator.deciders import OfflineScenarioDecider
from unstuck.operator.export import EXPORT_MEDIA_TYPES, export_bytes
from unstuck.operator.ledger import ScenarioBinding, ScenarioLedger
from unstuck.operator.modes import resolve_mode
from unstuck.operator.projection import evidence_panel, run_view, spine_for
from unstuck.operator.scenarios import (
    LOCAL_ARM,
    Scenario,
    preobserved_evidence,
    resolve_scenario,
)
from unstuck.operator.security import LocalGuard
from unstuck.storage import LocalRunRepository, Snapshot

STATIC_ROOT = Path(__file__).parent / "static"
#: The only assets served, by exact name, each with the one type it is served
#: as. The name is the key, so a new asset cannot inherit another's type by
#: accident and there is no traversal surface.
STATIC_MEDIA_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}
STATIC_ASSETS = tuple(STATIC_MEDIA_TYPES)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STATUS_FOR_REASON: dict[OperatorReason, int] = {
    OperatorReason.NON_LOOPBACK_HOST: 500,
    OperatorReason.HOST_REJECTED: 403,
    OperatorReason.ORIGIN_REJECTED: 403,
    OperatorReason.MEDIA_TYPE_REJECTED: 415,
    OperatorReason.ARGUMENT_REJECTED: 400,
    OperatorReason.RUN_NOT_FOUND: 404,
    OperatorReason.NODE_NOT_FOUND: 404,
    OperatorReason.LIVE_MODE_BLOCKED: 409,
    OperatorReason.REPLAY_UNAVAILABLE: 409,
    OperatorReason.EXPORT_FORMAT_REJECTED: 400,
    OperatorReason.RETENTION_UNDECIDABLE: 409,
    OperatorReason.ASSET_NOT_FOUND: 404,
    OperatorReason.SCENARIO_REJECTED: 400,
    OperatorReason.SCENARIO_MISMATCH: 409,
    OperatorReason.SCENARIO_BINDING_UNREADABLE: 500,
    OperatorReason.EVALUATION_PATH_REJECTED: 400,
    OperatorReason.EVALUATION_EXPORT_INVALID: 409,
}


def _refusal(fault: OperatorFault, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "status": "refused",
        "reason_code": fault.reason.value,
        "detail": fault.detail,
    }
    return JSONResponse(
        status_code=STATUS_FOR_REASON.get(fault.reason, 400), content=body | extra
    )


class OfflineRunService:
    """Starts and reads offline runs through the real controller.

    The service owns one receipt repository per scenario, so the Decision
    Receipt a call writes is still there for the next call on that scenario and
    the `decider_memory` arm has something to look up at all. Namespaces are
    isolated per arm and per scenario, so one scenario's history can never
    reach another's, and no evaluation namespace is ever touched from here.

    The scenario a run was created from is persisted beside the run authority,
    in the same repository boundary, by `ScenarioLedger`. That binding is what
    makes the idempotency key scenario-bound: an exact duplicate resumes its
    own run, and the same key offered with another scenario is refused.

    Nothing here decides an outcome, selects a tool, or authorizes a repair.
    """

    def __init__(self, repository: LocalRunRepository, clock: Clock):
        self.repository = repository
        self.clock = clock
        self.settings = Settings(baseline_id=LOCAL_ARM)
        self._receipts: dict[str, ReceiptRepository] = {}
        self.ledger = ScenarioLedger(repository.root)

    def binding(self, run_id: UUID) -> ScenarioBinding | None:
        """The recorded scenario authority of one run, read from disk."""
        return self.ledger.read(run_id)

    def receipts(self, scenario: Scenario) -> ReceiptRepository:
        """The receipt store for one scenario. Created empty, then kept."""
        return self._receipts.setdefault(scenario.case_id, LocalReceiptRepository())

    def namespace(self, scenario: Scenario) -> ReceiptNamespace:
        return ReceiptNamespace(arm=LOCAL_ARM, case_id=scenario.case_id)

    def task(self, fixture: Fixture) -> tuple[TaskSpec, str]:
        return (
            TaskSpec(
                dataset_id=fixture.dataset_id, order_date_utc=fixture.order_date_utc
            ),
            fixture.dataset_version,
        )

    def observed(
        self, scenario: Scenario, fixture: Fixture
    ) -> tuple[EvidenceRecord, ...]:
        """Produce the evidence this scenario declares already observed.

        The records come from the real evidence engine, so the run re-observes
        facts it genuinely holds rather than facts this module invented. The
        production itself belongs to the scenario declaration, so every surface
        that starts a scenario starts it from the same records.
        """
        return preobserved_evidence(scenario, fixture, clock=self.clock)

    async def start(self, request: RunRequest) -> Snapshot:
        """Create and run one offline run, awaiting the real controller.

        This is awaited rather than wrapped in `asyncio.run`, because the route
        that calls it is already inside a running event loop.

        The scenario binding is recorded before the existing run can be
        returned, so a key reused for a different scenario is refused rather
        than silently answered with the first scenario's result. The binding
        itself is read back through `binding()`, from disk, so a reader of a
        run never depends on having been the caller that created it.
        """
        from unstuck.controller import Controller
        from unstuck.query import EvidenceEngine

        scenario = resolve_scenario(request.scenario)
        fixture = scenario.fixture()
        task, dataset_version = self.task(fixture)
        snapshot = self.repository.create_run(
            task, dataset_version, request.idempotency_key
        )
        self.ledger.bind(
            ScenarioBinding.of(
                snapshot.state.run_id, scenario, fixture, request.idempotency_key
            )
        )
        if snapshot.state.status != "created":
            # A duplicate invocation of the same scenario resumes the existing
            # run rather than starting a second one. Nothing is decided again.
            return self.repository.load_run(snapshot.state.run_id)
        initial = self.observed(scenario, fixture)
        engine = EvidenceEngine(fixture, clock=self.clock)
        try:
            controller = Controller(
                self.repository,
                engine,
                OfflineScenarioDecider(),
                clock=self.clock,
                settings=self.settings,
                initial_evidence=initial,
                receipts=self.receipts(scenario),
                namespace=self.namespace(scenario),
            )
            await controller.run(snapshot.state.run_id)
        finally:
            engine.close()
        return self.repository.load_run(snapshot.state.run_id)

    def load(self, run_id: UUID) -> Snapshot:
        try:
            return self.repository.load_run(run_id)
        except RuntimeFault as error:
            if error.reason is Reason.STORAGE_ERROR:
                raise OperatorFault(
                    OperatorReason.RUN_NOT_FOUND, str(run_id)
                ) from error
            raise


def create_app(
    *,
    repository: LocalRunRepository,
    clock: Clock | None = None,
    bind: BindAddress | None = None,
    redactor: Redactor | None = None,
    export_root: Path | None = None,
    export_file: str = EXPORT_FILENAME,
) -> FastAPI:
    """Build the local application. Loopback only, no CORS, guarded routes.

    `export_root` is the one directory a benchmark export may be read from, and
    it is explicit: with none configured the benchmark tab reports an empty
    corpus, which is the honest state. The export is resolved canonically
    inside that root and validated against this unit's consumer schema before
    any figure is displayed.
    """
    resolved_bind = bind or BindAddress()
    guard = LocalGuard(resolved_bind)
    cleaner = redactor or Redactor()
    service = OfflineRunService(repository, clock or SystemClock())
    app = FastAPI(
        title="Unstuck operator",
        description="Local, loopback-only operator surface. Offline evidence.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        try:
            if request.method in WRITE_METHODS:
                guard.write(request.headers)
            else:
                guard.read(request.headers)
        except OperatorFault as fault:
            return _refusal(fault)
        return await call_next(request)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        label = resolve_mode("offline")
        return JSONResponse(
            {
                "status": "ready",
                "bind": resolved_bind.authority,
                "mode": label.effective,
                "provider": label.provider,
                "classification": "synthetic",
                "live_checks": "Pending",
            }
        )

    @app.post("/api/runs")
    async def create_run(request: Request) -> JSONResponse:
        # The strict contracts accept a serialized UUID or datetime only on the
        # JSON path, so the raw body is validated rather than a parsed dict.
        try:
            parsed = RunRequest.model_validate_json(await request.body())
        except ValueError:
            return _refusal(
                OperatorFault(
                    OperatorReason.ARGUMENT_REJECTED,
                    "the request carries an unknown or malformed field",
                )
            )
        label = resolve_mode(parsed.mode)
        if label.blocked:
            try:
                label.require_runnable()
            except OperatorFault as fault:
                return _refusal(fault, gate=label.gate, requested=parsed.mode)
        snapshot = await service.start(parsed)
        view = run_view(
            snapshot,
            label,
            redactor=cleaner,
            binding=service.binding(snapshot.state.run_id),
        )
        return JSONResponse(view.model_dump(mode="json", exclude={"schema_version"}))

    @app.get("/api/runs/{run_id}")
    async def read_run(run_id: UUID) -> JSONResponse:
        snapshot = service.load(run_id)
        view = run_view(
            snapshot,
            resolve_mode("offline"),
            redactor=cleaner,
            binding=service.binding(run_id),
        )
        return JSONResponse(view.model_dump(mode="json", exclude={"schema_version"}))

    @app.get("/api/runs/{run_id}/spine")
    async def read_spine(run_id: UUID) -> JSONResponse:
        snapshot = service.load(run_id)
        nodes = [
            node.model_dump(mode="json", exclude={"schema_version"})
            for node in spine_for(snapshot)
        ]
        return JSONResponse({"run_id": str(run_id), "spine": nodes})

    @app.get("/api/runs/{run_id}/evidence/{node_id}")
    async def read_evidence(run_id: UUID, node_id: str) -> JSONResponse:
        snapshot = service.load(run_id)
        panel = evidence_panel(snapshot, node_id, redactor=cleaner)
        return JSONResponse(panel.model_dump(mode="json", exclude={"schema_version"}))

    @app.get("/api/benchmark")
    async def benchmark() -> JSONResponse:
        export = None if export_root is None else load_export(export_root, export_file)
        view = benchmark_view(export)
        return JSONResponse(view.model_dump(mode="json", exclude={"schema_version"}))

    @app.post("/api/exports")
    async def create_export(request: Request) -> Any:
        try:
            parsed = ExportRequest.model_validate_json(await request.body())
        except ValueError:
            return _refusal(
                OperatorFault(
                    OperatorReason.ARGUMENT_REJECTED,
                    "the request carries an unknown or malformed field",
                )
            )
        snapshot = service.load(parsed.run_id)
        body = export_bytes(
            snapshot, parsed, redactor=cleaner, binding=service.binding(parsed.run_id)
        )
        from fastapi.responses import Response

        return Response(
            content=body,
            media_type=EXPORT_MEDIA_TYPES[parsed.format],
            headers={
                "content-disposition": (
                    f'attachment; filename="unstuck-{parsed.run_id}.{parsed.format}"'
                )
            },
        )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(
            STATIC_ROOT / "index.html", media_type=STATIC_MEDIA_TYPES["index.html"]
        )

    @app.get("/static/{asset}")
    async def static_asset(asset: str) -> FileResponse:
        kind = STATIC_MEDIA_TYPES.get(asset)
        if kind is None:
            raise OperatorFault(OperatorReason.ASSET_NOT_FOUND, asset)
        return FileResponse(STATIC_ROOT / asset, media_type=kind)

    @app.exception_handler(OperatorFault)
    async def operator_fault(request: Request, exc: OperatorFault) -> JSONResponse:
        return _refusal(exc)

    return app


__all__ = [
    "STATIC_ASSETS",
    "STATIC_MEDIA_TYPES",
    "STATIC_ROOT",
    "STATUS_FOR_REASON",
    "OfflineRunService",
    "create_app",
]
