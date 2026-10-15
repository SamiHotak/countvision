"""Cameras added and removed in the web app (devices with ``manages_cameras``).

The local edition's built-in device works like this: the shop owner adds "Entrance, webcam 0" or
"Till, rtsp://..." in the browser, and the edge agent on the same PC starts counting it within
seconds (it gets the list with its long-poll, see camera_config.device_state). Removing a camera
stops it on the device; its numbers stay in the dashboards.

The address may contain the camera's password (rtsp://user:pass@...). It is stored encrypted
(saas/crypto.py, key from CV_SECRET_KEY) and shown on the page without the password. Only the
device gets it in clear text, over its own authenticated connection.

Cameras over the plan/licence limit are not sent to the device (they would only use CPU: their
numbers would not be stored anyway). Adding one more camera than the limit allows is refused.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas import crypto
from ...saas.errors import AppError, Conflict, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit, billing
from .. import live
from ..models import Camera, Device

log = logging.getLogger(__name__)

KINDS = ("webcam", "rtsp", "http")
MAX_PER_DEVICE = 16
_CREDENTIALS = re.compile(r"(://)[^/@\s]+@")


class SourceError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="invalid_source", status=422, fields={"address": message})


def redact(uri: str) -> str:
    """The address without user:password (shown on the page and in logs)."""
    return _CREDENTIALS.sub(r"\1***@", uri)


def check_source(kind: str, address: str) -> str:
    """Validate and normalise what the user typed. Returns the address the edge agent uses."""
    address = (address or "").strip()
    if kind not in KINDS:
        raise SourceError("Choose webcam, IP camera (RTSP) or HTTP / MJPEG stream.")
    if kind == "webcam":
        if not address.isdigit() or not 0 <= int(address) <= 9:
            raise SourceError("A webcam number is 0 to 9. The built-in webcam is usually 0, a USB webcam 1.")
        return str(int(address))
    if len(address) > 1000 or any(c.isspace() for c in address):
        raise SourceError("The address must not contain spaces.")
    allowed = ("rtsp://", "rtsps://") if kind == "rtsp" else ("http://", "https://")
    if not address.lower().startswith(allowed):
        example = ("rtsp://192.168.1.20:554/stream1" if kind == "rtsp"
                   else "http://192.168.1.30:8080/video")
        raise SourceError(f"The address must start with {allowed[0]} (for example {example}).")
    parts = urlsplit(address)
    if not parts.hostname:
        raise SourceError("The address has no camera host (an IP address like 192.168.1.20).")
    return address


def source_of(cam: Camera) -> dict[str, str] | None:
    """{"kind", "uri"} for the device, or None when it cannot be read (other secret key)."""
    if not cam.source_kind:
        return None
    uri = crypto.decrypt(cam.source_secret)
    if uri is None:
        return None
    return {"kind": cam.source_kind, "uri": uri}


def _managed(db: Session, org: Organization, device: Device) -> list[Camera]:
    return list(db.scalars(
        select(Camera).where(Camera.device_id == device.id, Camera.removed_at.is_(None),
                             Camera.source_kind.is_not(None))
        .order_by(Camera.created_at)))


def _check_device(device: Device) -> None:
    if device.revoked_at is not None:
        raise Conflict("This device was removed.", code="device_revoked")
    if not device.manages_cameras:
        raise Conflict("This device's cameras are set in its own config file (edge YAML), not here.",
                       code="not_managed")


def add_camera(db: Session, org: Organization, device: Device, actor: User, *, name: str, kind: str,
               address: str) -> Camera:
    _check_device(device)
    uri = check_source(kind, address)
    current = _managed(db, org, device)
    if len(current) >= MAX_PER_DEVICE:
        raise Conflict(f"At most {MAX_PER_DEVICE} cameras per device.", code="limit")
    for cam in current:
        src = source_of(cam)
        if src and src["kind"] == kind and src["uri"] == uri:
            raise Conflict(f"This camera is already added as '{cam.name}'.", code="duplicate_source",
                           fields={"address": "Already added"})
    if any(c.name.strip().lower() == name.strip().lower() for c in current):
        raise Conflict("Another camera already has this name.", code="duplicate_name",
                       fields={"name": "Name already used"})
    billing.check_limit(db, org.id, "cameras", adding=1)  # 402 when the plan/licence is full
    edge_id = "cam-" + secrets.token_hex(3)
    cam = Camera(org_id=org.id, device_id=device.id, edge_camera_id=edge_id, name=name.strip(),
                 name_locked=True, source_kind=kind, source_secret=crypto.encrypt(uri),
                 source_label=_label(kind, uri), state="starting")
    db.add(cam)
    db.flush()
    audit.record(db, "camera.added", actor=actor.id, org_id=org.id, target_type="camera", target_id=cam.id,
                 name=cam.name, source=cam.source_label, device=device.name)
    live.queue_bump(db, device.id)
    live.queue(db, org.id, {"type": "refresh", "device_id": str(device.id)})
    log.info("Camera %s (%s) added to device %s", cam.name, cam.source_label, device.id)
    return cam


def _label(kind: str, uri: str) -> str:
    if kind == "webcam":
        return f"Webcam {uri}"
    return redact(uri)[:300]


def change_source(db: Session, org: Organization, cam: Camera, actor: User, *, kind: str,
                  address: str) -> Camera:
    """New address (or new password) for a camera; the device restarts it."""
    _check_device(cam.device)
    if not cam.source_kind or cam.removed_at is not None:
        raise Conflict("This camera is not managed in the web app.", code="not_managed")
    uri = check_source(kind, address)
    cam.source_kind, cam.source_secret, cam.source_label = kind, crypto.encrypt(uri), _label(kind, uri)
    audit.record(db, "camera.source_changed", actor=actor.id, org_id=org.id, target_type="camera",
                 target_id=cam.id, name=cam.name, source=cam.source_label)
    live.queue_bump(db, cam.device_id)
    return cam


def remove_camera(db: Session, org: Organization, cam: Camera, actor: User) -> None:
    _check_device(cam.device)
    if cam.removed_at is not None:
        return
    cam.removed_at = utcnow()
    cam.state = "removed"
    audit.record(db, "camera.removed", actor=actor.id, org_id=org.id, target_type="camera", target_id=cam.id,
                 name=cam.name)
    live.queue_bump(db, cam.device_id)
    live.queue(db, org.id, {"type": "refresh", "device_id": str(cam.device_id)})
    from . import plan_limits

    plan_limits.refresh_paused(db, org.id)  # a paused camera may now get the free slot


def get_managed(db: Session, org: Organization, camera_id) -> Camera:
    cam = db.scalar(select(Camera).where(Camera.org_id == org.id, Camera.id == camera_id))
    if cam is None or cam.removed_at is not None:
        raise NotFound("Camera not found.")
    return cam


def device_cameras(db: Session, device: Device) -> list[dict[str, Any]]:
    """What a managing device must run: every camera that is not removed and not paused."""
    out = []
    for cam in db.scalars(select(Camera).where(Camera.device_id == device.id, Camera.removed_at.is_(None),
                                               Camera.source_kind.is_not(None)).order_by(Camera.created_at)):
        if cam.plan_paused:
            continue
        src = source_of(cam)
        if src is None:
            log.warning("Camera %s: its address cannot be read (CV_SECRET_KEY changed?)", cam.id)
            continue
        out.append({"id": cam.edge_camera_id, "name": cam.name, "source": src})
    return out


def count(db: Session, device: Device) -> int:
    return db.scalar(select(func.count(Camera.id)).where(Camera.device_id == device.id,
                                                          Camera.removed_at.is_(None))) or 0
