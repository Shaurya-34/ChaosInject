"""FastAPI composition: the operator token, the origin and size checks, the chaos router and the built dashboard."""
import hmac
from uuid import uuid4
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from app.api.config import Settings
from app.api.errors import PortError, MESSAGES
from app.api.chaos_runs import create_router as chaos_router

def create_app(settings: Settings | None = None):
    settings = settings or Settings.environment()
    app = FastAPI(title="ChaosInject", version="1.0.0")
    app.state.settings = settings
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.origins), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type", "Idempotency-Key"], allow_credentials=False)
    def error(code, status):
        return JSONResponse({"code": code, "message": MESSAGES.get(code, "Operation refused."), "retryable": status in (429, 503), "correlation_id": str(uuid4()), "details": {}}, status_code=status)
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
    app.include_router(chaos_router(settings.chaos_execution_enabled))
    dist = Path(__file__).resolve().parents[2] / "frontend/dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")
        @app.get("/", include_in_schema=False)
        async def index(): return FileResponse(dist / "index.html")
    return app
