"""Sites: places with cameras. Members create and edit them, admins delete empty ones."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...saas.errors import Conflict, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit
from ..models import Camera, Device, Site

MAX_SITES = 200


def list_sites(db: Session, org: Organization) -> list[tuple[Site, int, int]]:
    """Sites with (device count, camera count), by name."""
    dev_count = (
        select(func.count(Device.id)).where(Device.site_id == Site.id, Device.revoked_at.is_(None))
        .correlate(Site).scalar_subquery()
    )
    cam_count = (
        select(func.count(Camera.id)).join(Device, Device.id == Camera.device_id)
        .where(Device.site_id == Site.id, Device.revoked_at.is_(None)).correlate(Site).scalar_subquery()
    )
    rows = db.execute(
        select(Site, dev_count, cam_count).where(Site.org_id == org.id).order_by(func.lower(Site.name))
    )
    return [(s, d or 0, c or 0) for s, d, c in rows]


def get_site(db: Session, org: Organization, site_id: uuid.UUID) -> Site:
    site = db.scalar(select(Site).where(Site.org_id == org.id, Site.id == site_id))
    if site is None:
        raise NotFound("Site not found.")
    return site


def create_site(db: Session, org: Organization, actor: User, *, name: str, timezone: str,
                address: str | None) -> Site:
    count = db.scalar(select(func.count(Site.id)).where(Site.org_id == org.id)) or 0
    if count >= MAX_SITES:
        raise Conflict(f"At most {MAX_SITES} sites per organization.", code="limit")
    site = Site(org_id=org.id, name=name, timezone=timezone, address=address)
    db.add(site)
    db.flush()
    audit.record(db, "site.created", actor=actor.id, org_id=org.id, target_type="site", target_id=site.id,
                 name=name)
    return site


def update_site(db: Session, site: Site, actor: User, changes: dict) -> Site:
    for key in ("name", "timezone", "address"):
        if key in changes and changes[key] is not None:
            setattr(site, key, changes[key])
    audit.record(db, "site.updated", actor=actor.id, org_id=site.org_id, target_type="site",
                 target_id=site.id, **{k: v for k, v in changes.items() if v is not None})
    return site


def delete_site(db: Session, site: Site, actor: User) -> None:
    active = db.scalar(select(func.count(Device.id)).where(Device.site_id == site.id)) or 0
    if active:
        raise Conflict("This site still has devices. Move or remove them first.", code="site_not_empty")
    audit.record(db, "site.deleted", actor=actor.id, org_id=site.org_id, target_type="site",
                 target_id=site.id, name=site.name)
    db.delete(site)
    db.flush()
