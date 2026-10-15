"""The FastAPI application.

Run locally:  uvicorn countvision_cloud.main:app --reload --port 8001
In Docker:    see cloud/compose.yaml (the web app proxies /api/* to this service).
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .logging_setup import setup_logging
from .product.routers import dashboards, device_api, integrations, live_api, manage, video
from .product.routers.public_api import public_app
from .saas.errors import install_error_handlers
from .saas.routers import admin, auth, billing, dev, dev_stripe, health, me, orgs
from .settings import get_settings

log = logging.getLogger(__name__)

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-countvision"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Local edition: renew the built-in edge agent's files if needed, start the in-process timers."""
    settings = get_settings()
    started = False
    if settings.is_local:
        from .db import db_session
        from .local import background, setup

        try:
            with db_session() as db:
                setup.ensure_edge_files(db)
        except Exception:  # the database may not be migrated yet: the API still starts
            log.exception("Could not check the built-in edge agent's files")
        if settings.local_background:
            background.start()
            started = True
    yield
    if started:
        from .local import background

        background.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings.log_level)
    docs = settings.environment != "production"
    app = FastAPI(
        title="CountVision API", version=__version__,
        docs_url="/api/docs" if docs else None, redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None, lifespan=lifespan,
    )
    install_error_handlers(app)

    @app.middleware("http")
    async def guard(request: Request, call_next) -> Response:
        # CSRF: every state-changing call must send a custom header. Browsers cannot add it to
        # cross-site form posts, and we do not allow cross-origin requests (no CORS).
        # The device API (/api/device/*) uses a Bearer token, not cookies, so it needs no CSRF header.
        path = request.url.path
        # The test fakes (/api/dev/telegram, /api/dev/webhook-sink) are called by our own server.
        # Stripe signs its webhooks; the fake Stripe pages post plain HTML forms.
        exempt = path.startswith(("/api/device/", "/api/dev/telegram/", "/api/dev/webhook-sink",
                                  "/api/billing/stripe-webhook", "/api/dev/stripe/"))
        if request.method in UNSAFE and path.startswith("/api/") and not exempt \
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

    routers = [health.router, auth.router, me.router, orgs.router, orgs.invite_router, manage.router,
               dashboards.router, integrations.router, video.router, device_api.router, live_api.router]
    if not settings.is_local:  # the local edition has a licence instead of Stripe, and no operator page
        routers += [billing.router, billing.webhook_router, admin.router]
    for router in routers:
        app.include_router(router)
    from .local import router as local_router

    app.include_router(local_router.public_router)
    if settings.is_local:
        app.include_router(local_router.router)
    app.mount("/api/v1", public_app)  # public API with its own docs at /api/v1/docs
    if settings.environment != "production" and settings.email_backend == "memory":
        app.include_router(dev.router)
        if not settings.is_local:
            app.include_router(dev_stripe.router)
        log.warning("Dev outbox enabled at /api/dev/outbox (memory email backend)")
    log.info("CountVision API %s (%s, %s edition), web %s, email %s/%s, google %s, billing %s", __version__,
             settings.environment, settings.edition, settings.web_url, settings.email_backend,
             settings.email_delivery,
             "on" if settings.google_enabled else "off",
             "licence key" if settings.is_local else
             ("on (Stripe TEST mode)" if settings.stripe_test_mode else "on (Stripe LIVE)")
             if settings.billing_enabled else "off")
    return app


app = create_app()
