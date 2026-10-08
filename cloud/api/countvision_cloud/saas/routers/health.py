"""/api/health: is the API up, can it reach the database and Redis."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ... import __version__
from ...db import get_engine
from ...redis_client import get_redis

HEARTBEAT_KEY = "cv:beat:last"  # written by countvision_cloud.tasks.heartbeat

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/api/health")
def health() -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # report, do not crash
        log.warning("Health: database error: %s", exc)
        checks["database"] = "error"
    try:
        get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        log.warning("Health: redis error: %s", exc)
        checks["redis"] = "error"
    ok = all(v == "ok" for v in checks.values())
    # Background worker: beat writes a timestamp every minute (information only, not part of "ok").
    try:
        last = get_redis().get(HEARTBEAT_KEY)
        checks["worker"] = "ok" if last and time.time() - int(last) < 180 else "stale"
    except Exception:
        checks["worker"] = "unknown"
    return JSONResponse({"status": "ok" if ok else "degraded", "version": __version__, "checks": checks},
                        status_code=200 if ok else 503)
