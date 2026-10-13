"""CountVision facts per organization for the operator's admin page (devices, sites, newest data)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.services import admin
from ..models import Device, Site
from .devices import ONLINE_AFTER


def org_facts(db: Session, org_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, Any]]:
    online_since = utcnow() - ONLINE_AFTER
    out: dict[uuid.UUID, dict[str, Any]] = {
        i: {"devices": 0, "devices_online": 0, "sites": 0, "last_data_at": None} for i in org_ids
    }
    rows = db.execute(
        select(
            Device.org_id,
            func.count(),
            func.count().filter(Device.last_seen_at >= online_since),
            func.max(Device.last_data_at),
        )
        .where(Device.org_id.in_(org_ids), Device.revoked_at.is_(None))
        .group_by(Device.org_id)
    )
    for org_id, n, online, last in rows:
        out[org_id].update(devices=n, devices_online=online, last_data_at=last.isoformat() if last else None)
    for org_id, n in db.execute(
        select(Site.org_id, func.count()).where(Site.org_id.in_(org_ids)).group_by(Site.org_id)
    ):
        out[org_id]["sites"] = n
    return out


admin.register_org_facts(org_facts)
