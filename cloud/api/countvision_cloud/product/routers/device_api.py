"""/api/device/*: the API for edge agents (no cookies; "Authorization: Bearer cvd_...")."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ...db import get_db, session_factory
from ...redis_client import async_redis
from ...saas.errors import AppError
from ...saas.models import Organization
from ...saas.services import ratelimit
from .. import live
from ..models import Device
from ..schemas import IngestIn, IngestOut, PairIn, PairOut
from ..services import camera_config, devices, ingest

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/device", tags=["device API (edge agents)"])

PAIR_ATTEMPTS = 10  # per IP and 15 minutes (codes have 8 random characters and expire in 15 min)


def current_device(authorization: str | None = Header(default=None),
                   db: Session = Depends(get_db)) -> Device:
    return devices.device_from_token(db, authorization)


@router.post("/pair", response_model=PairOut)
def pair(body: PairIn, request: Request, db: Session = Depends(get_db)) -> PairOut:
    """Exchange a one-time pairing code for this device's token (shown only once)."""
    ratelimit.limit("pair-ip", ratelimit.client_ip(request), PAIR_ATTEMPTS, 900,
                    "Too many pairing attempts. Wait 15 minutes.")
    device, token = devices.pair(db, code=body.code, edge_device_id=body.edge_device_id,
                                 agent_version=body.agent_version)
    org = db.get(Organization, device.org_id)
    out = PairOut(device_id=device.id, token=token, org_name=org.name if org else "",
                  site_name=device.site.name, device_name=device.name)
    db.commit()
    log.info("Device %s paired (%s)", device.id, body.edge_device_id)
    return out


@router.post("/ingest", response_model=IngestOut)
def upload(body: IngestIn, device: Device = Depends(current_device),
           db: Session = Depends(get_db)) -> IngestOut:
    """Upload a batch of numbers (and the device status). Safe to repeat."""
    result = ingest.ingest(db, device, body)
    db.commit()
    return result


@router.get("/me")
def whoami(device: Device = Depends(current_device), db: Session = Depends(get_db)) -> dict:
    """Lets the agent check its token (countvision-edge cloud-status)."""
    org = db.get(Organization, device.org_id)
    return {"device_id": str(device.id), "name": device.name, "site": device.site.name,
            "organization": org.name if org else None}


# --- config push and snapshots (Phase 3 C) ---------------------------------------------------------

POLL_STEP_S = 0.5


def _poll_auth(authorization: str | None) -> uuid.UUID:
    with session_factory()() as db:
        return devices.device_from_token(db, authorization).id


def _poll_state(device_id: uuid.UUID) -> dict:
    with session_factory()() as db:
        device = db.get(Device, device_id)
        assert device is not None
        state = camera_config.device_state(db, device)
    return state


@router.get("/poll")
async def poll(rev: int = Query(-1), wait: float = Query(25.0, ge=0, le=55),
               authorization: str | None = Header(default=None)) -> dict:
    """Long-poll: answers at once when the device's revision differs from ``rev``, else waits
    up to ``wait`` seconds for a change (new config or snapshot request). No DB connection is
    held while waiting."""
    device_id = await run_in_threadpool(_poll_auth, authorization)
    redis = async_redis()
    try:
        deadline = time.monotonic() + wait
        current = int(await redis.get(live.device_rev_key(device_id)) or 0)
        while current == rev and time.monotonic() < deadline:
            await asyncio.sleep(POLL_STEP_S)
            current = int(await redis.get(live.device_rev_key(device_id)) or 0)
    finally:
        await redis.aclose()
    if current == rev:
        return {"rev": current, "changed": False}
    state = await run_in_threadpool(_poll_state, device_id)
    return {"rev": current, "changed": True, **state}


@router.post("/snapshots/{request_id}", status_code=204)
async def upload_snapshot(request_id: str, request: Request,
                          authorization: str | None = Header(default=None)) -> Response:
    """The device answers a snapshot request: body = JPEG, or JSON {"error": "..."}."""
    body = await request.body()
    if len(body) > camera_config.MAX_SNAPSHOT_BYTES:
        raise AppError("Snapshot too large (max 2 MB).", code="too_large", status=413)

    def store() -> None:
        with session_factory()() as db:
            device = devices.device_from_token(db, authorization)
            if request.headers.get("content-type", "").startswith("application/json"):
                try:
                    error = str(json.loads(body or b"{}").get("error") or "unknown error")
                except ValueError:
                    error = "unknown error"
                camera_config.store_snapshot(device, request_id, image=None, error=error)
            else:
                camera_config.store_snapshot(device, request_id, image=body, error=None)

    await run_in_threadpool(store)
    return Response(status_code=204)
