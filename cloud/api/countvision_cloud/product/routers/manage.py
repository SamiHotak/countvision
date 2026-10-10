"""/api/orgs/{org_id}/sites, /devices, /pairing-codes, /cameras (logged-in users).

Roles: viewers read; members create sites, pairing codes and rename things; admins delete
sites and remove (revoke) or delete devices.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from ...db import get_db, utcnow
from ...saas.deps import OrgContext, org_access
from ...saas.errors import NotFound
from ...saas.models import Role
from ...saas.schemas import OkOut
from ..models import Device, Site
from ..schemas import (
    CameraConfigDoc,
    CameraDetailOut,
    CameraOut,
    CameraPatch,
    DeviceDetailOut,
    DeviceOut,
    DevicePatch,
    PairingCodeIn,
    PairingCodeOut,
    SiteIn,
    SiteOut,
    SitePatch,
    SnapshotOut,
)
from ..services import camera_config, devices, sites, stats

router = APIRouter(prefix="/api/orgs/{org_id}", tags=["sites and devices"])


def _site_out(site: Site, n_dev: int, n_cam: int) -> SiteOut:
    return SiteOut(id=site.id, name=site.name, timezone=site.timezone, address=site.address,
                   created_at=site.created_at, device_count=n_dev, camera_count=n_cam)


# --- sites ----------------------------------------------------------------------------------

@router.get("/sites", response_model=list[SiteOut])
def list_sites(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> list[SiteOut]:
    return [_site_out(s, d, c) for s, d, c in sites.list_sites(db, ctx.org)]


@router.post("/sites", response_model=SiteOut, status_code=201)
def create_site(body: SiteIn, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                db: Session = Depends(get_db)) -> SiteOut:
    site = sites.create_site(db, ctx.org, ctx.user, name=body.name, timezone=body.timezone,
                             address=body.address)
    db.commit()
    return _site_out(site, 0, 0)


@router.patch("/sites/{site_id}", response_model=SiteOut)
def update_site(site_id: uuid.UUID, body: SitePatch, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                db: Session = Depends(get_db)) -> SiteOut:
    site = sites.get_site(db, ctx.org, site_id)
    sites.update_site(db, site, ctx.user, body.model_dump(exclude_unset=True))
    db.commit()
    row = next(r for r in sites.list_sites(db, ctx.org) if r[0].id == site.id)
    return _site_out(*row)


@router.delete("/sites/{site_id}", response_model=OkOut)
def delete_site(site_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                db: Session = Depends(get_db)) -> OkOut:
    sites.delete_site(db, sites.get_site(db, ctx.org, site_id), ctx.user)
    db.commit()
    return OkOut(message="Site deleted.")


# --- pairing codes ----------------------------------------------------------------------------

def _code_out(row, code: str | None = None) -> PairingCodeOut:
    return PairingCodeOut(id=row.id, code=code, site_id=row.site_id, device_name=row.device_name,
                          expires_at=row.expires_at, status=devices.code_status(row), device_id=row.device_id)


@router.post("/pairing-codes", response_model=PairingCodeOut, status_code=201)
def create_pairing_code(body: PairingCodeIn, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                        db: Session = Depends(get_db)) -> PairingCodeOut:
    site = sites.get_site(db, ctx.org, body.site_id)
    row, code = devices.create_pairing_code(db, ctx.org, ctx.user, site, body.device_name)
    db.commit()
    return _code_out(row, code)


@router.get("/pairing-codes/{code_id}", response_model=PairingCodeOut)
def pairing_code_status(code_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                        db: Session = Depends(get_db)) -> PairingCodeOut:
    """The web app polls this to show "Device connected" as soon as the code was used."""
    return _code_out(devices.get_pairing_code(db, ctx.org, code_id))


# --- devices ----------------------------------------------------------------------------------

def _device_out(d: Device) -> dict:
    now = utcnow()
    online = devices.is_online(d, now)
    return dict(
        id=d.id, name=d.name, site_id=d.site_id, site_name=d.site.name, edge_device_id=d.edge_device_id,
        agent_version=d.agent_version, online=online, last_seen_at=d.last_seen_at,
        last_data_at=d.last_data_at, paired_at=d.paired_at, revoked=d.revoked_at is not None,
        camera_count=len(d.cameras),
        cameras_online=sum(1 for c in d.cameras
                           if online and c.state in ("running", "paused") and c.connected is not False),
        detector=(d.status or {}).get("detector"), upload=(d.status or {}).get("upload"),
    )


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> list[DeviceOut]:
    return [DeviceOut(**_device_out(d)) for d in devices.list_devices(db, ctx.org)]


@router.get("/devices/{device_id}", response_model=DeviceDetailOut)
def get_device(device_id: uuid.UUID, ctx: OrgContext = Depends(org_access()),
               db: Session = Depends(get_db)) -> DeviceDetailOut:
    d = devices.get_device(db, ctx.org, device_id)
    online = devices.is_online(d)
    today = stats.event_totals(db, [c.id for c in d.cameras], stats.local_midnight(d.site.timezone))
    batches, rows = devices.batch_stats(db, d, utcnow() - timedelta(hours=24))
    cams = [
        CameraOut(id=c.id, edge_camera_id=c.edge_camera_id, name=c.name,
                  state=c.state if online else "offline" if c.state else None,
                  connected=c.connected if online else None, fps=c.fps if online else None,
                  reconnects=c.reconnects, alert=c.alert, lines=c.lines or [], zones=c.zones or [],
                  last_seen_at=c.last_seen_at, today=today.get(c.id, {}),
                  desired_version=c.desired_version, applied_version=c.applied_version,
                  config_error=c.config_error)
        for c in d.cameras
    ]
    return DeviceDetailOut(**_device_out(d), cameras=cams, site_timezone=d.site.timezone,
                           batches_24h=batches, rows_24h=rows)


@router.patch("/devices/{device_id}", response_model=DeviceOut)
def update_device(device_id: uuid.UUID, body: DevicePatch, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                  db: Session = Depends(get_db)) -> DeviceOut:
    d = devices.get_device(db, ctx.org, device_id)
    devices.update_device(db, ctx.org, d, ctx.user, name=body.name, site_id=body.site_id)
    db.commit()
    db.refresh(d)
    return DeviceOut(**_device_out(devices.get_device(db, ctx.org, device_id)))


@router.post("/devices/{device_id}/revoke", response_model=OkOut)
def revoke_device(device_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                  db: Session = Depends(get_db)) -> OkOut:
    devices.revoke_device(db, devices.get_device(db, ctx.org, device_id), ctx.user)
    db.commit()
    return OkOut(message="Device removed. It can no longer send data; its numbers stay.")


@router.delete("/devices/{device_id}", response_model=OkOut)
def delete_device(device_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                  db: Session = Depends(get_db)) -> OkOut:
    devices.delete_device(db, devices.get_device(db, ctx.org, device_id), ctx.user)
    db.commit()
    return OkOut(message="Device and its data deleted.")


@router.patch("/cameras/{camera_id}", response_model=OkOut)
def rename_camera(camera_id: uuid.UUID, body: CameraPatch, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                  db: Session = Depends(get_db)) -> OkOut:
    devices.rename_camera(db, ctx.org, camera_id, ctx.user, body.name)
    db.commit()
    return OkOut(message="Camera renamed.")


# --- camera config and snapshots (Phase 3 C) ------------------------------------------------------

@router.get("/cameras/{camera_id}", response_model=CameraDetailOut)
def get_camera(camera_id: uuid.UUID, ctx: OrgContext = Depends(org_access()),
               db: Session = Depends(get_db)) -> CameraDetailOut:
    return camera_config.camera_detail(db, camera_config.get_camera(db, ctx.org, camera_id))


@router.put("/cameras/{camera_id}/config", response_model=CameraDetailOut)
def save_camera_config(camera_id: uuid.UUID, body: CameraConfigDoc,
                       ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                       db: Session = Depends(get_db)) -> CameraDetailOut:
    """Save lines, zones, classes, anchor and schedule; the device applies them within seconds."""
    cam = camera_config.get_camera(db, ctx.org, camera_id)
    camera_config.save_config(db, cam, ctx.user, body)
    db.commit()
    camera_config.notify_device(cam.device_id)
    return camera_config.camera_detail(db, camera_config.get_camera(db, ctx.org, camera_id))


@router.post("/cameras/{camera_id}/snapshot", response_model=SnapshotOut, status_code=202)
def take_snapshot(camera_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                  db: Session = Depends(get_db)) -> SnapshotOut:
    """Ask the device for ONE pixelated picture (kept 10 minutes in memory, for drawing lines)."""
    cam = camera_config.get_camera(db, ctx.org, camera_id)
    req = camera_config.request_snapshot(db, cam, ctx.user)
    db.commit()
    camera_config.notify_device(cam.device_id)
    return SnapshotOut(request_id=req, status="pending")


@router.get("/cameras/{camera_id}/snapshot/{request_id}", response_model=SnapshotOut)
def snapshot_status(camera_id: uuid.UUID, request_id: str, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                    db: Session = Depends(get_db)) -> SnapshotOut:
    out, _ = camera_config.snapshot_status(camera_config.get_camera(db, ctx.org, camera_id), request_id)
    return out


@router.get("/cameras/{camera_id}/snapshot/{request_id}/image")
def snapshot_image(camera_id: uuid.UUID, request_id: str, ctx: OrgContext = Depends(org_access(Role.MEMBER)),
                   db: Session = Depends(get_db)) -> Response:
    out, image = camera_config.snapshot_status(camera_config.get_camera(db, ctx.org, camera_id), request_id)
    if image is None:
        raise NotFound("The snapshot is not ready.", code="snapshot_not_ready")
    return Response(image, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
