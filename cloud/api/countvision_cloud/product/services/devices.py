"""Devices: pairing with a one-time code, device tokens, status, revoke.

Pairing flow:
1. A member creates a pairing code in the web app (site + device name). Code like K7QF-3MXP,
   valid 15 minutes, one use, only its SHA-256 is stored.
2. On the edge computer: countvision-edge pair --url https://app... --code K7QF-3MXP
3. The API checks the code and answers with a NEW random device token (shown only once,
   stored hashed). The agent keeps it in <data_dir>/cloud.json and sends it as
   "Authorization: Bearer <token>" with every upload.
4. Revoking a device makes the token useless at once; its data stays.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ...db import utcnow
from ...saas import security
from ...saas.errors import AppError, Conflict, NotAuthenticated, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit
from ..models import Camera, Device, IngestBatch, PairingCode, Site

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: easy to read and type
CODE_LENGTH = 8
CODE_MINUTES = 15
TOKEN_PREFIX = "cvd_"
ONLINE_AFTER = timedelta(seconds=90)  # no contact for longer = offline
MAX_DEVICES = 100
MAX_OPEN_CODES = 20


def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def format_code(raw: str) -> str:
    return f"{raw[:4]}-{raw[4:]}"


def is_online(device: Device, now: datetime | None = None) -> bool:
    if device.revoked_at is not None or device.last_seen_at is None:
        return False
    return (now or utcnow()) - device.last_seen_at <= ONLINE_AFTER


# --- pairing codes --------------------------------------------------------------------------

def create_pairing_code(db: Session, org: Organization, actor: User, site: Site,
                        device_name: str) -> tuple[PairingCode, str]:
    now = utcnow()
    open_codes = db.scalar(
        select(func.count(PairingCode.id)).where(
            PairingCode.org_id == org.id, PairingCode.used_at.is_(None), PairingCode.expires_at > now)
    ) or 0
    if open_codes >= MAX_OPEN_CODES:
        raise Conflict("Too many open pairing codes. Wait 15 minutes or use one of them.", code="limit")
    devices = db.scalar(
        select(func.count(Device.id)).where(Device.org_id == org.id, Device.revoked_at.is_(None))) or 0
    if devices >= MAX_DEVICES:
        raise Conflict(f"At most {MAX_DEVICES} devices per organization.", code="limit")
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    row = PairingCode(org_id=org.id, site_id=site.id, device_name=device_name,
                      code_hash=security.token_hash(raw), created_by=actor.id,
                      expires_at=now + timedelta(minutes=CODE_MINUTES))
    db.add(row)
    db.flush()
    audit.record(db, "device.pairing_code_created", actor=actor.id, org_id=org.id, target_type="site",
                 target_id=site.id, device_name=device_name)
    return row, format_code(raw)


def get_pairing_code(db: Session, org: Organization, code_id: uuid.UUID) -> PairingCode:
    row = db.scalar(select(PairingCode).where(PairingCode.org_id == org.id, PairingCode.id == code_id))
    if row is None:
        raise NotFound("Pairing code not found.")
    return row


def code_status(row: PairingCode, now: datetime | None = None) -> str:
    if row.used_at is not None:
        return "used"
    if row.expires_at <= (now or utcnow()):
        return "expired"
    return "pending"


def pair(db: Session, *, code: str, edge_device_id: str, agent_version: str | None) -> tuple[Device, str]:
    """Exchange a pairing code for a device token (called by the edge agent)."""
    raw = normalize_code(code)
    row = None
    if len(raw) == CODE_LENGTH:
        row = db.scalar(
            select(PairingCode).where(PairingCode.code_hash == security.token_hash(raw)).with_for_update()
        )
    if row is None or code_status(row) != "pending":
        raise AppError("This pairing code is wrong, already used or expired. Create a new one in the "
                       "web app (Devices -> Add device).", code="invalid_code", status=400)
    site = db.get(Site, row.site_id)
    org = db.get(Organization, row.org_id)
    if site is None or org is None:
        raise AppError("The site of this code no longer exists.", code="invalid_code", status=400)
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    device = Device(org_id=org.id, site_id=site.id, name=row.device_name, edge_device_id=edge_device_id,
                    token_hash=security.token_hash(token), agent_version=agent_version, paired_at=now,
                    last_seen_at=now)
    db.add(device)
    db.flush()
    row.used_at = now
    row.device_id = device.id
    audit.record(db, "device.paired", actor=row.created_by, org_id=org.id, target_type="device",
                 target_id=device.id, name=device.name, site=site.name, edge_device_id=edge_device_id)
    return device, token


# --- device auth ----------------------------------------------------------------------------

def device_from_token(db: Session, authorization: str | None) -> Device:
    """The device for an "Authorization: Bearer cvd_..." header, or 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise NotAuthenticated("Missing device token.", code="device_token_missing")
    token = authorization[7:].strip()
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        raise NotAuthenticated("Invalid device token.", code="device_token_invalid")
    device = db.scalar(select(Device).where(Device.token_hash == security.token_hash(token)))
    if device is None:
        raise NotAuthenticated("Invalid device token.", code="device_token_invalid")
    if device.revoked_at is not None:
        raise NotAuthenticated("This device was removed from the organization. Pair it again.",
                               code="device_revoked")
    return device


# --- management -------------------------------------------------------------------------------

def list_devices(db: Session, org: Organization) -> list[Device]:
    return list(db.scalars(
        select(Device).options(selectinload(Device.cameras), selectinload(Device.site))
        .where(Device.org_id == org.id).order_by(Device.revoked_at.is_not(None), func.lower(Device.name))
    ))


def get_device(db: Session, org: Organization, device_id: uuid.UUID) -> Device:
    device = db.scalar(
        select(Device).options(selectinload(Device.cameras), selectinload(Device.site))
        .where(Device.org_id == org.id, Device.id == device_id)
    )
    if device is None:
        raise NotFound("Device not found.")
    return device


def update_device(db: Session, org: Organization, device: Device, actor: User, *, name: str | None,
                  site_id: uuid.UUID | None) -> Device:
    changes: dict[str, str] = {}
    if name and name != device.name:
        changes["name"] = name
        device.name = name
    if site_id and site_id != device.site_id:
        site = db.scalar(select(Site).where(Site.org_id == org.id, Site.id == site_id))
        if site is None:
            raise NotFound("Site not found.")
        device.site_id = site.id
        changes["site"] = site.name
    if changes:
        audit.record(db, "device.updated", actor=actor.id, org_id=org.id, target_type="device",
                     target_id=device.id, **changes)
    return device


def revoke_device(db: Session, device: Device, actor: User) -> None:
    if device.revoked_at is not None:
        return
    device.revoked_at = utcnow()
    audit.record(db, "device.revoked", actor=actor.id, org_id=device.org_id, target_type="device",
                 target_id=device.id, name=device.name)


def delete_device(db: Session, device: Device, actor: User) -> None:
    """Delete a REVOKED device with its cameras and all their counting data."""
    if device.revoked_at is None:
        raise Conflict("Remove (revoke) the device first.", code="device_active")
    audit.record(db, "device.deleted", actor=actor.id, org_id=device.org_id, target_type="device",
                 target_id=device.id, name=device.name)
    db.delete(device)
    db.flush()


def rename_camera(db: Session, org: Organization, camera_id: uuid.UUID, actor: User, name: str) -> Camera:
    camera = db.scalar(select(Camera).where(Camera.org_id == org.id, Camera.id == camera_id))
    if camera is None:
        raise NotFound("Camera not found.")
    old = camera.name
    camera.name = name
    camera.name_locked = True  # the name from the edge config no longer overwrites it
    audit.record(db, "camera.renamed", actor=actor.id, org_id=org.id, target_type="camera",
                 target_id=camera.id, old=old, new=name)
    return camera


def batch_stats(db: Session, device: Device, since: datetime) -> tuple[int, int]:
    row = db.execute(
        select(func.count(IngestBatch.id), func.coalesce(func.sum(IngestBatch.rows), 0))
        .where(IngestBatch.device_id == device.id, IngestBatch.received_at >= since)
    ).one()
    return int(row[0]), int(row[1])
