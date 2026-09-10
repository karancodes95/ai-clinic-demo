"""FastAPI application factory and cross-cutting concerns: lifespan (DB pool),
CORS, security headers, and the error-shape handler. Routes live in app.api.*"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db
from app.api import admin, admin_auth, catalog, chat, leads, meta
from app.config import settings
from app.core import sessions


# How often the background TTL sweep would run if enabled. Kept for future use.
_CLEANUP_INTERVAL_SECONDS = 3600


async def _cleanup_loop() -> None:
    """Periodically delete sessions past their TTL. Currently not started (see
    lifespan) but kept ready to re-enable."""
    log = logging.getLogger("cleanup")
    while True:
        try:
            deleted = await sessions.cleanup_expired()
            if deleted:
                log.info("cleanup: deleted %d expired sessions", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cleanup loop iteration failed")
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    await db.init_pool()
    # Cleanup loop disabled: session/event rows are kept indefinitely.
    # `expires_at` is still enforced at the chat endpoint as a token-validity
    # stamp, but rows are never deleted.
    try:
        yield
    finally:
        await db.close_pool()


app = FastAPI(
    title="Clinic AI Analyst",
    lifespan=lifespan,
    # Interactive docs / OpenAPI are disabled so a casual visit to /docs on a
    # public origin doesn't hand over the entire API contract. Generate the
    # schema locally if needed:
    #   python -c "from app.main import app; import json; print(json.dumps(app.openapi()))"
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Routes. Admin routers carry their own /admin prefix.
app.include_router(meta.router)
app.include_router(catalog.router)
app.include_router(chat.router)
app.include_router(leads.router)
app.include_router(admin.router)
app.include_router(admin_auth.router)

# Explicit CORS allowlist when set; "*" only when unset (dev). Production .env
# should set ALLOWED_ORIGINS to the deployed frontend origin(s).
_origins = (
    [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
    if settings.allowed_origins
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Quota-Used", "X-Quota-Cap", "X-Quota-Resets-At"],
)


# Defense-in-depth headers on every response. This service returns JSON/SSE
# only (no HTML), so the policy is strict and same-origin.
_CSP = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'"
)


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(HTTPException)
async def _http_exc_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize HTTPException to the {error, message} contract."""
    code = "http_error"
    if exc.status_code == 401:
        code = "unauthorized"
    elif exc.status_code == 400:
        code = "bad_request"
    elif exc.status_code == 404:
        code = "not_found"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": code, "message": str(exc.detail)},
    )
