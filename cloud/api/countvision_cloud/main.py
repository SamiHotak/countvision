"""The FastAPI application.

Run locally:  uvicorn countvision_cloud.main:app --reload --port 8001
In Docker:    see cloud/compose.yaml (the web app proxies /api/* to this service).
"""

from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .logging_setup import setup_logging
from .product.routers import device_api, live_api, manage
from .saas.errors import install_error_handlers
from .saas.routers import auth, dev, health, me, orgs
from .settings import get_settings

log = logging.getLogger(__name__)

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-countvision"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    docs = settings.environment != "production"
    app = FastAPI(
        title="CountVision API", version=__version__,
        docs_url="/api/docs" if docs else None, redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        # CSRF: every state-changing call must send a custom header. Browsers cannot add it to
        # cross-site form posts, and we do not allow cross-origin requests (no CORS).
        # The device API (/api/device/*) uses a Bearer token, not cookies, so it needs no CSRF header.
        path = request.url.path
        if request.method in UNSAFE and path.startswith("/api/") and not path.startswith("/api/device/") \
                and request.headers.get(CSRF_HEADER) != "1":
            return JSONResponse({"error": {"code": "csrf", "message": "Missing X-CountVision header."}},
                                status_code=403)
        started = time.perf_counter()
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        ms = (time.perf_counter() - started) * 1000
        if path != "/api/health" and not path.endswith("/live") and path != "/api/device/poll":
            log.info("%s %s -> %s (%.0f ms)", request.method, request.url.path, response.status_code, ms)
        return response

    for router in (health.router, auth.router, me.router, orgs.router, orgs.invite_router, manage.router,
                   device_api.router, live_api.router):
        app.include_router(router)
    if settings.environment != "production" and settings.email_backend == "memory":
        app.include_router(dev.router)
        log.warning("Dev outbox enabled at /api/dev/outbox (memory email backend)")
    log.info("CountVision API %s (%s), web %s, email %s/%s, google %s", __version__, settings.environment,
             settings.web_url, settings.email_backend, settings.email_delivery,
             "on" if settings.google_enabled else "off")
    return app


app = create_app()
