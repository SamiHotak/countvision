"""Live updates for the browser: GET /api/orgs/{org_id}/live (Server-Sent Events).

Messages (JSON in ``data:``): count (one line crossing), device (state after every upload),
config (saved / applied), snapshot (ready / error), refresh (many changes: reload numbers).
The stream ends after ``max_s`` seconds (default 10 minutes); EventSource reconnects by itself.
Only numbers and names are sent, never pictures.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from ...db import session_factory
from ...redis_client import async_redis
from ...saas.deps import session_token
from ...saas.errors import NotAuthenticated
from ...saas.models import Role
from ...saas.services import auth, orgs
from .. import live

router = APIRouter(tags=["live"])
KEEPALIVE_S = 15.0


def _check_access(org_id: uuid.UUID, token: str | None) -> None:
    """Auth with a short-lived DB session (the stream itself holds no DB connection)."""
    with session_factory()() as db:
        user = auth.user_from_token(db, token)
        if user is None:
            raise NotAuthenticated("Please log in.")
        orgs.access(db, org_id, user, Role.VIEWER)


@router.get("/api/orgs/{org_id}/live")
async def live_stream(org_id: uuid.UUID, request: Request,
                      max_s: float = Query(600.0, ge=1, le=3600)) -> StreamingResponse:
    await run_in_threadpool(_check_access, org_id, session_token(request))

    async def events():
        redis = async_redis()
        pubsub = redis.pubsub()
        await pubsub.subscribe(live.org_channel(org_id))
        started = last_send = time.monotonic()
        try:
            yield "retry: 3000\n\n"
            yield f"data: {json.dumps({'type': 'hello', 'server_time': time.time()})}\n\n"
            while time.monotonic() - started < max_s:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg.get("type") == "message":
                    yield f"data: {msg['data']}\n\n"
                    last_send = time.monotonic()
                elif time.monotonic() - last_send > KEEPALIVE_S:
                    yield ": keepalive\n\n"
                    last_send = time.monotonic()
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()
                await redis.aclose()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
