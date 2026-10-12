"""The public API: /api/v1 (read-only, API key). Its own small FastAPI app with public docs at
/api/v1/docs, so customers can connect CountVision numbers to their own systems (POS, BI, staff
planning) without logging in.

All times are in the site's time zone with the UTC offset (ISO 8601). Counts are the site's visitor
lines (as chosen on the dashboard) unless ``by=line``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import __version__
from ...db import get_db, utcnow
from ...saas.errors import AppError, NotFound, install_error_handlers
from ...saas.models import Organization
from ..models import Camera, Device, Site
from ..services import alerts, analytics, api_keys, devices

DESCRIPTION = """
Read the numbers of your CountVision organization: sites, live counts, visitors per hour or day,
zones, cameras and alerts.

**Authentication**: create a key on the API page of the web app (admins), then send it with every
request: `Authorization: Bearer cvk_...`. Keys are read-only. Limit: 120 requests per minute per key.

**Times**: in the site's time zone, ISO 8601 with offset. `from` / `to` are local dates (both
included). **Privacy**: only numbers exist in CountVision; there are no pictures in this API.

**Webhooks** (push instead of pull): create a webhook on the Alerts page. Every request is signed,
see the API page for how to check the signature.
"""

public_app = FastAPI(
    title="CountVision public API",
    version="1",
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)
install_error_handlers(public_app)


class Ctx:
    def __init__(self, org: Organization) -> None:
        self.org = org


def key_auth(request: Request, db: Session = Depends(get_db)) -> Ctx:
    org, _ = api_keys.org_from_key(db, request)
    return Ctx(org)


# --- models (they make the docs readable) -------------------------------------------------------


class SiteV1(BaseModel):
    id: uuid.UUID
    name: str
    timezone: str
    address: str | None


class LiveV1(BaseModel):
    site_id: uuid.UUID
    date: date
    visitors_in: int
    visitors_out: int
    inside_estimate: int
    same_time_last_week_in: int
    change_pct: float | None
    occupancy_now: int | None
    queue_now: int | None
    zones: list[dict]
    as_of: datetime


class CameraV1(BaseModel):
    id: uuid.UUID
    name: str
    site_id: uuid.UUID
    device_id: uuid.UUID
    state: str | None
    online: bool
    fps: float | None
    lines: list[str]
    zones: list[str]
    last_seen_at: datetime | None


class AlertV1(BaseModel):
    id: int
    rule_id: uuid.UUID | None
    site_id: uuid.UUID | None
    rule_name: str
    kind: str
    status: str
    value: float | None
    message: str
    created_at: datetime


# --- helpers -------------------------------------------------------------------------------------


def _site(db: Session, org: Organization, site_id: uuid.UUID) -> Site:
    site = db.scalar(select(Site).where(Site.org_id == org.id, Site.id == site_id))
    if site is None:
        raise NotFound("Site not found.")
    return site


def _aware(naive: datetime, tz: str) -> datetime:
    return naive.replace(tzinfo=ZoneInfo(tz))


# --- routes -----------------------------------------------------------------------------------


@public_app.get("/sites", response_model=list[SiteV1], summary="List sites")
def list_sites(ctx: Ctx = Depends(key_auth), db: Session = Depends(get_db)) -> list[SiteV1]:
    rows = db.scalars(select(Site).where(Site.org_id == ctx.org.id).order_by(Site.name))
    return [SiteV1(id=s.id, name=s.name, timezone=s.timezone, address=s.address) for s in rows]


@public_app.get("/sites/{site_id}/live", response_model=LiveV1, summary="Today and right now")
def live(site_id: uuid.UUID, ctx: Ctx = Depends(key_auth), db: Session = Depends(get_db)) -> LiveV1:
    """Visitors today (since local midnight, updated within seconds), compared with the same time
    last week, and people in zones now (newest minute, at most 5 minutes old)."""
    site = _site(db, ctx.org, site_id)
    now = utcnow()
    t = analytics.site_today(db, site, now)
    sc = analytics.site_cameras(db, site)
    occ = analytics.occupancy_now(db, sc, now)
    return LiveV1(
        site_id=site.id,
        date=analytics.local_today(site.timezone, now),
        visitors_in=t["today_in"],
        visitors_out=t["today_out"],
        inside_estimate=t["inside_estimate"],
        same_time_last_week_in=t["compare_in"],
        change_pct=t["change_pct"],
        occupancy_now=t["occupancy"],
        queue_now=t["queue"],
        zones=[
            {"camera_id": str(o["camera_id"]), "zone": o["zone"], "kind": o["kind"], "value": o["value"]}
            for o in occ
        ],
        as_of=now.astimezone(ZoneInfo(site.timezone)),
    )


@public_app.get("/sites/{site_id}/counts", summary="Visitors per minute, hour or day")
def counts(
    site_id: uuid.UUID,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    bucket: Literal["minute", "hour", "day"] = "hour",
    by: Literal["total", "line", "class"] = "total",
    ctx: Ctx = Depends(key_auth),
    db: Session = Depends(get_db),
) -> dict:
    """IN/OUT per time bucket. ``by=total``: the site's visitor lines added up; ``by=line``: every
    line separately (with ``counts_as_visitors``); ``by=class``: visitor lines per class (person, car
    ...). Minute buckets for at most 7 days. Empty buckets are left out."""
    site = _site(db, ctx.org, site_id)
    p = analytics.parse_period(site.timezone, from_, to)
    if bucket == "minute" and p.days > 7:
        raise AppError("Minute buckets for at most 7 days.", code="bad_period", status=422)
    sc = analytics.site_cameras(db, site)
    raw = analytics.line_hours(db, sc.ids, p.start, min(p.end, utcnow()), site.timezone, unit=bucket)
    sel = analytics.visitor_filter(site)
    acc: dict[tuple, list[int]] = {}
    tot = [0, 0]
    for (cam, line, cls, t), (n_in, n_out) in raw.items():
        visitor = analytics.is_visitor(sel, cam, line)
        if by == "line":
            if cls != analytics.ALL:
                continue
            key = (t, str(cam), line, visitor)
        elif by == "class":
            if cls == analytics.ALL or not visitor:
                continue
            key = (t, cls)
        else:
            if cls != analytics.ALL or not visitor:
                continue
            key = (t,)
        a = acc.setdefault(key, [0, 0])
        a[0] += n_in
        a[1] += n_out
        if visitor and cls == analytics.ALL:
            tot[0] += n_in
            tot[1] += n_out
    data = []
    for key in sorted(acc, key=lambda k: tuple(str(x) for x in k)):
        row: dict = {"t": _aware(key[0], site.timezone).isoformat()}
        if by == "line":
            row.update(
                camera_id=key[1],
                camera_name=sc.names.get(uuid.UUID(key[1]), "?"),
                line=key[2],
                counts_as_visitors=key[3],
            )
        elif by == "class":
            row["class_name"] = key[1]
        row.update({"in": acc[key][0], "out": acc[key][1]})
        data.append(row)
    return {
        "site_id": str(site.id),
        "timezone": site.timezone,
        "from": p.first.isoformat(),
        "to": p.last.isoformat(),
        "bucket": bucket,
        "by": by,
        "total_in": tot[0],
        "total_out": tot[1],
        "data": data,
    }


@public_app.get("/sites/{site_id}/zones", summary="Zone occupancy, queue and visits")
def zones(
    site_id: uuid.UUID,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    bucket: Literal["minute", "hour", "day"] = "hour",
    ctx: Ctx = Depends(key_auth),
    db: Session = Depends(get_db),
) -> dict:
    """Per zone and time bucket: average and highest number of people (or people waiting for queue
    zones), visits and average visit length."""
    site = _site(db, ctx.org, site_id)
    p = analytics.parse_period(site.timezone, from_, to)
    if bucket == "minute" and p.days > 7:
        raise AppError("Minute buckets for at most 7 days.", code="bad_period", status=422)
    sc = analytics.site_cameras(db, site)
    raw = analytics.zone_hours(db, sc.ids, p.start, min(p.end, utcnow()), site.timezone, unit=bucket)
    data = []
    for (cam, zone, t), acc in sorted(raw.items(), key=lambda kv: (kv[0][2], str(kv[0][0]), kv[0][1])):
        data.append(
            {
                "t": _aware(t, site.timezone).isoformat(),
                "camera_id": str(cam),
                "camera_name": sc.names.get(cam, "?"),
                "zone": zone,
                "kind": sc.zone_kinds.get((cam, zone), "area"),
                "occupancy_avg": None if acc.occ_avg is None else round(acc.occ_avg, 2),
                "occupancy_max": acc.occ_max,
                "queue_avg": None if acc.queue_avg is None else round(acc.queue_avg, 2),
                "queue_max": acc.queue_max,
                "visits": acc.visits,
                "dwell_avg_s": None if acc.dwell_avg is None else round(acc.dwell_avg, 1),
            }
        )
    return {
        "site_id": str(site.id),
        "timezone": site.timezone,
        "from": p.first.isoformat(),
        "to": p.last.isoformat(),
        "bucket": bucket,
        "data": data,
    }


@public_app.get("/cameras", response_model=list[CameraV1], summary="List cameras and their state")
def cameras(ctx: Ctx = Depends(key_auth), db: Session = Depends(get_db)) -> list[CameraV1]:
    rows = db.execute(
        select(Camera, Device)
        .join(Device, Device.id == Camera.device_id)
        .where(Camera.org_id == ctx.org.id, Device.revoked_at.is_(None))
        .order_by(Camera.name)
    ).all()
    now = utcnow()
    out = []
    for cam, dev in rows:
        online = devices.is_online(dev, now)
        out.append(
            CameraV1(
                id=cam.id,
                name=cam.name,
                site_id=dev.site_id,
                device_id=dev.id,
                state=cam.state if online else "offline",
                online=online,
                fps=cam.fps if online else None,
                lines=cam.lines or [],
                zones=cam.zones or [],
                last_seen_at=cam.last_seen_at,
            )
        )
    return out


@public_app.get("/alerts", response_model=list[AlertV1], summary="Alert history (newest first)")
def alert_history(
    since: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    ctx: Ctx = Depends(key_auth),
    db: Session = Depends(get_db),
) -> list[AlertV1]:
    rows = alerts.history(db, ctx.org, limit, since or (utcnow() - timedelta(days=30)))
    return [
        AlertV1(
            id=e.id,
            rule_id=e.rule_id,
            site_id=e.site_id,
            rule_name=e.rule_name,
            kind=e.kind,
            status=e.status,
            value=e.value,
            message=e.message,
            created_at=e.created_at,
        )
        for e in rows
    ]


@public_app.get("/", include_in_schema=False)
def index() -> dict:
    return {"name": "CountVision public API", "version": "1", "server": __version__, "docs": "/api/v1/docs"}
