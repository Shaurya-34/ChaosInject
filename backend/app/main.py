"""FastAPI composition: owners implement policy, metrics and runtime authority."""
from contextlib import asynccontextmanager
import hmac
from uuid import uuid4
from pathlib import Path
from fastapi import FastAPI, Depends, Request, Query, Header
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from app.contracts import *
from app.api.config import Settings, configured_ports
from app.api.ports import Ports
from app.api.errors import PortError, MESSAGES
from app.api.evidence import checked_evidence, report

class ApproveRequest(Contract):
    proposal_hash: Annotated[str, Field(min_length=64, max_length=64)]
class StartRequest(Contract):
    approval_id: str

def create_app(settings: Settings | None = None, ports: Ports | None = None):
    settings = settings or Settings.environment()
    ports = ports or configured_ports(settings)
    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in list(getattr(ports.runtime, "tasks", [])): task.cancel()
    app = FastAPI(title="Bosch Resilience AI", version="1.0.0", lifespan=lifespan)
    app.state.ports, app.state.settings = ports, settings
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.origins), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], allow_credentials=False)
    def error(code, status):
        return JSONResponse(ErrorBody(code=code, message=MESSAGES.get(code, "Operation refused."), retryable=status in (429, 503), correlation_id=str(uuid4()), details={}).model_dump(mode="json"), status_code=status)
    @app.exception_handler(PortError)
    async def port_error(request, exc): return error(exc.code, exc.status)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc): return error("INVALID_REQUEST", 422)
    @app.exception_handler(HTTPException)
    async def http_error(request, exc): return error("NOT_FOUND" if exc.status_code == 404 else "INVALID_REQUEST", exc.status_code)
    @app.exception_handler(Exception)
    async def unknown_error(request, exc): return error("DEPENDENCY_UNAVAILABLE", 503)
    @app.middleware("http")
    async def safe_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in settings.origins: return error("ORIGIN_REJECTED", 403)
        public_asset = request.method == "GET" and (request.url.path == "/" or request.url.path.startswith("/assets/"))
        if not public_asset and request.method != "OPTIONS" and not hmac.compare_digest(request.headers.get("authorization", ""), "Bearer " + settings.operator_token): return error("UNAUTHORIZED", 401)
        try: length = int(request.headers.get("content-length", "0"))
        except ValueError: return error("INVALID_REQUEST", 400)
        if length > 65536: return error("BODY_TOO_LARGE", 413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536: return error("BODY_TOO_LARGE", 413)
        request._body = bytes(body)
        try:
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        except Exception: return error("DEPENDENCY_UNAVAILABLE", 503)
    async def actor(): return "local-operator"
    def mutation_guard():
        if settings.mode in ("replay", "demo") or (settings.mode == "fixture" and not settings.fake_runtime_enabled): raise PortError("MUTATIONS_DISABLED")
    def provenance(value):
        if hasattr(value, "source") and value.source != ("live" if settings.mode == "demo" else settings.mode): raise PortError("DEPENDENCY_UNAVAILABLE", 503)
        return value
    def key(value):
        if not value or len(value) > 128 or not value.isascii(): raise PortError("INVALID_REQUEST", 422)
        return value
    @app.get("/api/v1/health", response_model=Health)
    async def health():
        demo_mode = settings.mode == "demo"
        target_ok = (await ports.telemetry.probe())["status"] == "healthy" if demo_mode else True
        return Health(source="live" if demo_mode else settings.mode, status="ready" if target_ok else "degraded", dependencies={"platform": True, "telemetry": target_ok, "intelligence": True, "runtime_safety": ports.runtime_safety_ready}, sandbox_id=settings.sandbox_id, context=settings.context, namespace=settings.namespace, mutations_enabled=settings.mode == "live" or settings.fake_runtime_enabled, sql_execution_enabled=ports.sql_execution_ready and settings.mode == "live", limitations=["Local HTTP prevention and deterministic SQL analysis; no cluster faults, SQL executor or external LLM. Learner starts cold and has no SQL outcomes to train on."] if demo_mode else [] if settings.mode == "live" else ["Synthetic fixtures; no enrolled cluster or live evidence"] if settings.mode == "fixture" else ["Recorded data only; no execution"])
    @app.get("/api/v1/topology", response_model=TopologySnapshot)
    async def topology(): return provenance(await ports.platform.get_topology(settings.sandbox_id))
    @app.get("/api/v1/state", response_model=SystemStateSnapshot)
    async def state(): return provenance(await ports.telemetry.get_state(settings.sandbox_id))
    @app.get("/api/v1/experiments/catalog", response_model=Catalog)
    async def catalog(): return provenance(await ports.runtime.catalog())
    @app.post("/api/v1/experiments/plan", response_model=list[ExperimentProposal])
    async def plan():
        if settings.mode == "demo": return []
        return [provenance(p) for p in await ports.intelligence.plan(await topology(), await state(), await catalog(), {})]
    @app.post("/api/v1/experiments/validate", response_model=ValidationResult)
    async def validate(proposal: ExperimentProposal):
        if settings.mode == "replay": raise PortError("REPLAY_READ_ONLY")
        provenance(proposal)
        return await ports.runtime.validate(proposal)
    @app.post("/api/v1/experiments/{proposal_id}/approve", response_model=ApprovalRecord)
    async def approve(proposal_id: str, body: ApproveRequest, who=Depends(actor)):
        mutation_guard()
        return await ports.runtime.approve(proposal_id, who, body.proposal_hash)
    @app.post("/api/v1/experiments/{proposal_id}/start", status_code=202, response_model=RunHandle)
    async def start(proposal_id: str, body: StartRequest, idempotency_key: str | None = Header(None), who=Depends(actor)):
        mutation_guard()
        return provenance(await ports.runtime.start(proposal_id, body.approval_id, key(idempotency_key), who))
    @app.get("/api/v1/runs/{run_id}", response_model=RunStatus)
    async def run(run_id: str): return provenance(await ports.runtime.status(run_id))
    @app.get("/api/v1/runs/{run_id}/events", response_model=list[RunEvent])
    async def events(run_id: str, after_sequence: int = Query(0, ge=0)):
        return [provenance(e) for e in (await ports.runtime.events(run_id, after_sequence))[-128:]]
    @app.post("/api/v1/runs/{run_id}/stop", status_code=202, response_model=StopReceipt)
    async def stop(run_id: str, who=Depends(actor)):
        mutation_guard()
        return provenance(await ports.runtime.stop(run_id, who))
    @app.post("/api/v1/emergency-stop", status_code=202, response_model=StopReceipt)
    async def emergency_stop(who=Depends(actor)):
        if settings.mode == "replay": raise PortError("REPLAY_READ_ONLY")
        return provenance(await ports.runtime.emergency_stop(settings.sandbox_id, who))
    @app.post("/api/v1/safety/reset", response_model=CleanupSummary)
    async def reset(who=Depends(actor)):
        if settings.mode == "replay": raise PortError("REPLAY_READ_ONLY")
        r = await ports.runtime.reconcile(settings.sandbox_id)
        if r.cleanup_status not in ("verified", "not_applicable") or r.recovery_status != "verified" or r.unresolved_run_ids: raise PortError("CONFLICT")
        return provenance(await ports.runtime.reset(settings.sandbox_id, who))
    @app.get("/api/v1/learning/status", response_model=LearningStatus)
    async def learning_status(): return provenance(await ports.intelligence.get_learning_status())
    @app.get("/api/v1/runs/{run_id}/analysis", response_model=AnalysisResult)
    async def analysis(run_id: str):
        s = await run(run_id)
        if not s.summary: raise PortError("CONFLICT")
        a = provenance(await ports.intelligence.analyze_run(s.summary, await topology()))
        checked_evidence(s.summary, a)
        return a
    @app.get("/api/v1/reports/{run_id}", response_class=HTMLResponse)
    async def download_report(run_id: str):
        s = await run(run_id)
        if not s.summary: raise PortError("CONFLICT")
        return HTMLResponse(report(s.summary, await analysis(run_id)), headers={"Content-Disposition": 'attachment; filename="resilience-findings.html"', "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'", "X-Content-Type-Options": "nosniff"})
    @app.get("/api/v1/actions/{path:path}")
    @app.post("/api/v1/actions/{path:path}")
    async def actions_disabled(path: str): raise PortError("ACTIONS_DISABLED")
    from app.adapters.demo import DemoInjectionRequest, DemoInjectionResult
    @app.post("/api/v1/demo/injection", response_model=DemoInjectionResult)
    async def demo_injection(body: DemoInjectionRequest, who=Depends(actor)):
        if settings.mode != "demo": raise PortError("MUTATIONS_DISABLED")
        return await ports.runtime.inject_attempt(body, who)
    @app.get("/api/v1/demo/status", response_model=DemoInjectionResult | None)
    async def demo_status():
        if settings.mode != "demo": raise PortError("MUTATIONS_DISABLED")
        return ports.runtime.last_result
    from app.runtime.demo_contract import DemoSafetyStatus
    @app.get("/api/v1/demo/safety", response_model=DemoSafetyStatus)
    async def demo_safety():
        if settings.mode != "demo": raise PortError("MUTATIONS_DISABLED")
        return DemoSafetyStatus(source="live", stop_latched=ports.runtime.latched, last_result=ports.runtime.last_result)
    from app.api.chaos_runs import create_router as chaos_router
    app.include_router(chaos_router(settings.chaos_execution_enabled))
    dist = Path(__file__).resolve().parents[2] / "frontend/dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        @app.get("/", include_in_schema=False)
        async def index(): return FileResponse(dist / "index.html")
    return app
