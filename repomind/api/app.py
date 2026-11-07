"""The application: routes, the checks a local web server needs, and the built web client.

It is meant for one person on their own machine, and a web page they visit elsewhere can still
send requests to it. So it answers only to its own host names, which stops DNS rebinding, and
state-changing requests must come from its own origin as JSON, which stops cross-site requests.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from repomind.api.errors import ApiError
from repomind.api.routes import router
from repomind.api.services import Services, public_message
from repomind.errors import RepoMindError

logger = logging.getLogger("repomind.api")

ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
_STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
_MAX_BODY_BYTES = 64 * 1024
_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status, headers=headers)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return urlparse(origin).netloc == request.headers.get("host", "")


def create_app(services: Services) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            services.prepare_database()
        except RepoMindError as exc:
            # Stay up, so that the page can say what is wrong.
            logger.warning("Database not ready: %s", public_message(str(exc)))
        yield

    app = FastAPI(
        title="RepoMind AI",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.services = services

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in _STATE_CHANGING:
            if not _same_origin(request):
                return _error(403, "cross_origin", "Requests from another site are not accepted.")
            if request.headers.get("content-type", "").split(";")[0].strip() != "application/json" and request.method != "DELETE":
                return _error(415, "json_required", "This endpoint accepts JSON only.")
            declared = request.headers.get("content-length", "0")
            if declared.isdigit() and int(declared) > _MAX_BODY_BYTES:
                return _error(413, "too_large", "This request is too large.")
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    # Added after the guard, so that it runs first, and a wrong Host header gets no further.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RepoMindError)
    async def engine_error(_request: Request, exc: RepoMindError) -> JSONResponse:
        if "Cannot connect to PostgreSQL" in str(exc):
            return _error(503, "database_unavailable", "The database is not running. Start it with: docker compose up -d db")
        return _error(400, "request_failed", public_message(str(exc)))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [f"{'.'.join(str(part) for part in error['loc'][1:])}: {error['msg']}" for error in exc.errors()]
        return _error(422, "invalid_request", "; ".join(problems) or "This request is not valid.")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, "http_error", str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unexpected error", exc_info=exc)
        return _error(500, "internal_error", "Something went wrong on the server.")

    app.include_router(router)
    _serve_web_client(app, services.web_root)
    return app


def _serve_web_client(app: FastAPI, web_root: Path | None) -> None:
    """Any path that is not the API gets the same page, and the client's router takes it from there."""
    index = web_root / "index.html" if web_root else None
    if web_root and (web_root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=web_root / "assets"), name="assets")

    @app.get("/api/{rest:path}", include_in_schema=False)
    def unknown_api(rest: str) -> JSONResponse:
        return _error(404, "not_found", "There is no such API endpoint.")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        icon = web_root / "favicon.svg" if web_root else None
        if icon and icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return _error(404, "not_found", "No icon.")

    @app.get("/{rest:path}", include_in_schema=False)
    def page(rest: str):
        if index and index.is_file():
            return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})
        return JSONResponse(
            {"code": "web_client_not_built", "message": "The web client is not built. Run: npm --prefix web install && npm --prefix web run build"},
            status_code=503,
        )
