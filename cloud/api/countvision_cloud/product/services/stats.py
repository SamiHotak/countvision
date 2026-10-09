"""Small read queries for the device page (full dashboards come in Phase 4)."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ..models import LineCount


def local_midnight(tz: str, now: datetime | None = None) -> datetime:
    """Start of 'today' in the site's time zone, as an aware datetime."""
    zone = ZoneInfo(tz)
    local = (now or utcnow()).astimezone(zone)
    return datetime.combine(local.date(), time.min, tzinfo=zone)


def line_totals(db: Session, camera_ids: list[uuid.UUID], since: datetime,
                until: datetime | None = None) -> dict[uuid.UUID, dict[str, dict[str, int]]]:
    """IN/OUT per camera and line since a time (all classes together)."""
    if not camera_ids:
        return {}
    stmt = (
        select(LineCount.camera_id, LineCount.line, func.sum(LineCount.in_count),
               func.sum(LineCount.out_count))
        .where(LineCount.camera_id.in_(camera_ids), LineCount.class_name == "*",
               LineCount.window_start >= since)
        .group_by(LineCount.camera_id, LineCount.line)
    )
    if until is not None:
        stmt = stmt.where(LineCount.window_start < until)
    out: dict[uuid.UUID, dict[str, dict[str, int]]] = {}
    for cam, line, n_in, n_out in db.execute(stmt):
        out.setdefault(cam, {})[line] = {"in": int(n_in or 0), "out": int(n_out or 0)}
    return out


def day_window(tz: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    start = local_midnight(tz, now)
    return start, start + timedelta(days=1)
