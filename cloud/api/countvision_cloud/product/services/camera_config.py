"""Camera config from the cloud editor, the device long-poll, and on-request snapshots.

Config flow: the editor saves a CameraConfigDoc as ``desired_config`` with a new
``desired_version`` -> the device's revision counter in Redis goes up -> the device, waiting in
GET /api/device/poll, gets the new config within ~0.5 s -> it applies it to the running camera
and reports ``config_version`` with its next upload -> ``applied_version`` = desired = done.

Snapshot flow (privacy): only when a member clicks "Take snapshot". The request goes to the
device the same way; the device pixelates every detected person/vehicle, scales the frame
down and sends ONE JPEG. The cloud keeps it 10 minutes in Redis memory only (never in the
database, never on disk) and logs who asked for it. A device can forbid snapshots
(privacy.snapshots: false in its config).
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...redis_client import get_redis, get_redis_bytes
from ...saas.errors import Conflict, Forbidden, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit
from .. import live
from ..models import Camera, Device, Site
from ..schemas import CameraConfigDoc, CameraDetailOut, SnapshotOut
from . import devices, stats

log = logging.getLogger(__name__)

SNAPSHOT_TTL_S = 600  # the picture is gone from Redis after 10 minutes
SNAPSHOT_REQUEST_TTL_S = 120
MAX_SNAPSHOT_BYTES = 2_000_000


def _meta_key(req: str) -> str:
    return f"cv:snap:{req}:meta"


def _img_key(req: str) -> str:
    return f"cv:snap:{req}:img"


def get_camera(db: Session, org: Organization, camera_id: uuid.UUID) -> Camera:
    cam = db.scalar(
        select(Camera).options(selectinload(Camera.device).selectinload(Device.site))
        .where(Camera.org_id == org.id, Camera.id == camera_id)
    )
    if cam is None:
        raise NotFound("Camera not found.")
    return cam


def effective_config(cam: Camera) -> tuple[CameraConfigDoc | None, str]:
    """(config to show in the editor, where it comes from)."""
    for source, raw in (("cloud", cam.desired_config), ("device", cam.reported_config)):
        if raw:
            try:
                return CameraConfigDoc.model_validate(raw), source
            except ValueError:
                log.warning("Camera %s: stored %s config is not valid", cam.id, source)
    return None, "none"


def camera_detail(db: Session, cam: Camera) -> CameraDetailOut:
    device: Device = cam.device
    site: Site = device.site
    online = devices.is_online(device)
    doc, source = effective_config(cam)
    today = stats.event_totals(db, [cam.id], stats.local_midnight(site.timezone)).get(cam.id, {})
    return CameraDetailOut(
        id=cam.id, edge_camera_id=cam.edge_camera_id, name=cam.name, device_id=device.id,
        device_name=device.name, device_online=online, site_name=site.name, site_timezone=site.timezone,
        state=cam.state if online else "offline", fps=cam.fps if online else None, config=doc,
        config_source=source, desired_version=cam.desired_version, applied_version=cam.applied_version,
        config_error=cam.config_error, snapshots_allowed=cam.snapshots_allowed,
        frame_width=cam.frame_width, frame_height=cam.frame_height, today=today,
    )


def save_config(db: Session, cam: Camera, actor: User, doc: CameraConfigDoc) -> Camera:
    """Store the new config for the device. The caller commits, then calls notify_device."""
    if cam.device.revoked_at is not None:
        raise Conflict("This device was removed. Its cameras cannot be changed.", code="device_revoked")
    cam.desired_config = doc.model_dump()
    cam.desired_version = (cam.desired_version or 0) + 1
    cam.config_error = None
    audit.record(db, "camera.config_saved", actor=actor.id, org_id=cam.org_id, target_type="camera",
                 target_id=cam.id, camera=cam.name, version=cam.desired_version,
                 lines=len(doc.lines), zones=len(doc.zones))
    live.queue(db, cam.org_id, {"type": "config", "camera_id": str(cam.id),
                                "desired_version": cam.desired_version,
                                "applied_version": cam.applied_version})
    return cam


def notify_device(device_id: uuid.UUID) -> None:
    live.bump_device(device_id)


# --- snapshots ----------------------------------------------------------------------------------

def request_snapshot(db: Session, cam: Camera, actor: User) -> str:
    device = cam.device
    if device.revoked_at is not None:
        raise Conflict("This device was removed.", code="device_revoked")
    if not devices.is_online(device):
        raise Conflict("The device is offline. A snapshot needs the device to be online.",
                       code="device_offline")
    if cam.snapshots_allowed is False:
        raise Forbidden("Snapshots are switched off on this device (privacy.snapshots: false). "
                        "You can still edit lines on an empty grid.", code="snapshots_disabled")
    req = secrets.token_hex(12)
    r = get_redis()
    pipe = r.pipeline()
    pipe.hset(live.device_snaps_key(device.id), req, cam.edge_camera_id)
    pipe.expire(live.device_snaps_key(device.id), SNAPSHOT_REQUEST_TTL_S)
    pipe.set(_meta_key(req), json.dumps({"status": "pending", "camera_id": str(cam.id),
                                         "device_id": str(device.id)}), ex=SNAPSHOT_TTL_S)
    pipe.execute()
    audit.record(db, "camera.snapshot_requested", actor=actor.id, org_id=cam.org_id, target_type="camera",
                 target_id=cam.id, camera=cam.name)
    return req


def _meta(req: str) -> dict[str, Any] | None:
    raw = get_redis().get(_meta_key(req)) if len(req) <= 64 else None
    return json.loads(raw) if raw else None


def snapshot_status(cam: Camera, req: str) -> tuple[SnapshotOut, bytes | None]:
    meta = _meta(req)
    if meta is None or meta.get("camera_id") != str(cam.id):
        raise NotFound("This snapshot does not exist (any more). Snapshots are kept 10 minutes.",
                       code="snapshot_not_found")
    status = meta.get("status", "pending")
    image = get_redis_bytes().get(_img_key(req)) if status == "ready" else None
    if status == "ready" and image is None:
        raise NotFound("This snapshot has expired.", code="snapshot_not_found")
    return SnapshotOut(request_id=req, status=status, message=meta.get("message")), image


def store_snapshot(device: Device, req: str, *, image: bytes | None, error: str | None) -> None:
    """Called by the device with the JPEG (or an error message)."""
    r = get_redis()
    edge_cam = r.hget(live.device_snaps_key(device.id), req) if len(req) <= 64 else None
    meta = _meta(req)
    if edge_cam is None or meta is None or meta.get("device_id") != str(device.id):
        raise NotFound("Unknown or expired snapshot request.", code="snapshot_not_found")
    r.hdel(live.device_snaps_key(device.id), req)
    if error is not None or not image:
        meta.update(status="error", message=(error or "The device sent no picture.")[:300])
    elif len(image) > MAX_SNAPSHOT_BYTES or not image.startswith(b"\xff\xd8"):
        meta.update(status="error", message="The device sent an invalid picture.")
    else:
        get_redis_bytes().set(_img_key(req), image, ex=SNAPSHOT_TTL_S)
        meta.update(status="ready")
    r.set(_meta_key(req), json.dumps(meta), ex=SNAPSHOT_TTL_S)
    live.publish(device.org_id, {"type": "snapshot", "camera_id": meta["camera_id"], "request_id": req,
                                 "status": meta["status"]})
    if meta["status"] == "error":
        log.info("Snapshot %s failed on the device: %s", req, meta.get("message"))


# --- device long-poll ----------------------------------------------------------------------------

def device_state(db: Session, device: Device) -> dict[str, Any]:
    """Everything a device must do now: configs to apply and pending snapshot requests."""
    tz = device.site.timezone if device.site else "UTC"
    cameras = [
        {"id": c.edge_camera_id, "version": c.desired_version, "config": c.desired_config, "timezone": tz}
        for c in db.scalars(select(Camera).where(Camera.device_id == device.id))
        if c.desired_config is not None and c.desired_version > 0
    ]
    pending = get_redis().hgetall(live.device_snaps_key(device.id))
    snaps = [{"request_id": req, "camera_id": cam} for req, cam in pending.items()]
    return {"cameras": cameras, "snapshots": snaps}


def device_rev(device_id: uuid.UUID) -> int:
    raw = get_redis().get(live.device_rev_key(device_id))
    return int(raw or 0)


def check_doc(raw: Any) -> CameraConfigDoc | None:
    """Validate a config reported by the device (ignored when invalid)."""
    if not raw:
        return None
    try:
        return CameraConfigDoc.model_validate(raw)
    except ValueError:
        return None

