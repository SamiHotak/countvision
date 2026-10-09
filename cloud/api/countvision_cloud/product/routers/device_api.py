"""/api/device/*: the API for edge agents (no cookies; "Authorization: Bearer cvd_...")."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from ...db import get_db
from ...saas.models import Organization
from ...saas.services import ratelimit
from ..models import Device
from ..schemas import IngestIn, IngestOut, PairIn, PairOut
from ..services import devices, ingest

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
