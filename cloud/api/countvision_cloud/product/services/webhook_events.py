"""Webhook event streams besides alerts: ``crossing`` (every line crossing, in batches) and
``counts.hourly`` (one summary per site and finished local hour)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...redis_client import get_redis
from ...saas.services import billing
from ..models import Camera, Device, NotificationChannel, Site
from . import analytics, notify

log = logging.getLogger(__name__)

CROSSINGS_PER_DELIVERY = 500
HOURLY_DELAY = timedelta(minutes=5)  # minute rows of the last minutes arrive a bit later


def subscribers(db: Session, org_id, event: str) -> list[NotificationChannel]:
    rows = db.scalars(
        select(NotificationChannel).where(
            NotificationChannel.org_id == org_id,
            NotificationChannel.kind == "webhook",
            NotificationChannel.enabled.is_(True),
        )
    )
    return [c for c in rows if event in (c.config or {}).get("events", [])]


def queue_crossings(db: Session, device: Device, cams_by_id: dict, crossings: list[Any]) -> int:
    """Called by ingest with the NEW crossings of one upload (a repeated upload sends nothing)."""
    if not crossings:
        return 0
    hooks = subscribers(db, device.org_id, "crossing")
    if not hooks or not billing.entitlement(db, device.org_id).active:
        return 0  # no working plan: webhooks pause
    site = db.get(Site, device.site_id)
    items = [
        {
            "camera_id": str(e.camera_id),
            "camera_name": cams_by_id[e.camera_id].name if e.camera_id in cams_by_id else None,
            "line": e.name,
            "direction": e.direction,
            "class_name": e.class_name,
            "ts": e.ts.isoformat(),
        }
        for e in sorted(crossings, key=lambda e: e.ts)
    ]
    n = 0
    for i in range(0, len(items), CROSSINGS_PER_DELIVERY):
        part = items[i : i + CROSSINGS_PER_DELIVERY]
        for ch in hooks:
            notify.queue_webhook(
                db,
                ch,
                "crossing",
                {
                    "site_id": str(site.id),
                    "site_name": site.name,
                    "device_id": str(device.id),
                    "count": len(part),
                    "crossings": part,
                },
            )
            n += 1
    return n


def _last_hour(tz: str, now: datetime) -> tuple[datetime, datetime] | None:
    """The newest finished local hour (start, end), once its data is complete."""
    zone = ZoneInfo(tz)
    end = (now - HOURLY_DELAY).astimezone(zone).replace(minute=0, second=0, microsecond=0)
    return end - timedelta(hours=1), end


def hourly_payload(db: Session, site: Site, start: datetime, end: datetime) -> dict[str, Any]:
    sc = analytics.site_cameras(db, site)
    raw = analytics.line_hours(db, sc.ids, start, end, site.timezone, unit="minute")
    sel = analytics.visitor_filter(site)
    per_line: dict[tuple, list[int]] = {}
    total = [0, 0]
    for (cam, line, cls, _), (n_in, n_out) in raw.items():
        if cls != analytics.ALL:
            continue
        a = per_line.setdefault((cam, line), [0, 0])
        a[0] += n_in
        a[1] += n_out
        if analytics.is_visitor(sel, cam, line):
            total[0] += n_in
            total[1] += n_out
    return {
        "site_id": str(site.id),
        "site_name": site.name,
        "timezone": site.timezone,
        "hour_start": start.isoformat(),
        "hour_end": end.isoformat(),
        "in": total[0],
        "out": total[1],
        "lines": [
            {
                "camera_id": str(c),
                "camera_name": sc.names.get(c, "?"),
                "line": ln,
                "in": v[0],
                "out": v[1],
                "counts_as_visitors": analytics.is_visitor(sel, c, ln),
            }
            for (c, ln), v in sorted(per_line.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))
        ],
    }


def send_hourly(db: Session, now: datetime | None = None) -> int:
    """Celery beat (every 5 minutes): one ``counts.hourly`` event per subscribed webhook, site and
    finished hour. Remembers the last hour in Redis; a new webhook starts with the next hour."""
    now = now or utcnow()
    r = get_redis()
    sent = 0
    inactive = billing.inactive_org_ids(db)
    hooks = [
        h for h in db.scalars(
            select(NotificationChannel).where(
                NotificationChannel.kind == "webhook", NotificationChannel.enabled.is_(True)
            )
        )
        if h.org_id not in inactive
    ]
    for ch in hooks:
        if "counts.hourly" not in (ch.config or {}).get("events", []):
            continue
        site_rows = db.scalars(select(Site).where(Site.org_id == ch.org_id))
        for site in site_rows:
            has_cams = db.scalar(
                select(Camera.id)
                .join(Device, Device.id == Camera.device_id)
                .where(Device.site_id == site.id)
                .limit(1)
            )
            if not has_cams:
                continue
            start, end = _last_hour(site.timezone, now)
            key = f"cv:wh:hourly:{ch.id}:{site.id}"
            last = r.get(key)
            stamp = int(end.timestamp())
            if last is None:
                r.set(key, stamp, ex=7 * 86400)  # start with the NEXT hour
                continue
            if int(last) >= stamp:
                continue
            notify.queue_webhook(db, ch, "counts.hourly", hourly_payload(db, site, start, end))
            r.set(key, stamp, ex=7 * 86400)
            sent += 1
    db.commit()
    return sent
