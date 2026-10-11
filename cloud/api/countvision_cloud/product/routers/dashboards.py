"""/api/orgs/{org_id}/... dashboards, exports and report schedules (Phase 4 A).

Roles: everybody in the organization (also viewers) sees dashboards and downloads CSV/PDF.
Members choose the visitor lines and manage report emails.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from ...db import get_db, utcnow
from ...saas.deps import OrgContext, org_access
from ...saas.errors import AppError
from ...saas.models import Role
from ...saas.schemas import OkOut
from ...saas.services import audit
from ...settings import get_settings
from ..models import ReportSchedule
from ..reports import pdf
from ..schemas import (
    ReportRecipientOut,
    ReportScheduleIn,
    ReportScheduleOut,
    ReportSchedulePatch,
    SendNowOut,
    VisitorLinesIn,
)
from ..services import analytics, exports, reports, sample_data, sites

router = APIRouter(prefix="/api/orgs/{org_id}", tags=["dashboards and reports"])


def _period(site, first: date | None, last: date | None) -> analytics.Period:
    return analytics.parse_period(site.timezone, first, last)


@router.get("/dashboard")
def org_dashboard(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Overview: every site with visitors today vs. the same time last week and occupancy now."""
    now = utcnow()
    return {
        "sites": [analytics.site_today(db, s, now) for s, _, _ in sites.list_sites(db, ctx.org)],
        "generated_at": now,
    }


@router.get("/sites/{site_id}/dashboard")
def site_dashboard(
    site_id: uuid.UUID,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    ctx: OrgContext = Depends(org_access()),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    site = sites.get_site(db, ctx.org, site_id)
    return analytics.dashboard(db, site, _period(site, from_, to))


@router.put("/sites/{site_id}/visitor-lines")
def set_visitor_lines(
    site_id: uuid.UUID,
    body: VisitorLinesIn,
    ctx: OrgContext = Depends(org_access(Role.MEMBER)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    site = sites.get_site(db, ctx.org, site_id)
    site.visitor_lines = (
        None if body.lines is None else [{"camera_id": str(v.camera_id), "line": v.line} for v in body.lines]
    )
    audit.record(
        db,
        "site.visitor_lines_changed",
        actor=ctx.user.id,
        org_id=ctx.org.id,
        target_type="site",
        target_id=site.id,
        lines="all" if body.lines is None else len(body.lines),
    )
    db.commit()
    return {"visitor_lines": site.visitor_lines}


def _download(content: bytes, media: str, name: str) -> Response:
    return Response(
        content, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


@router.get("/sites/{site_id}/export.csv")
def export_csv(
    site_id: uuid.UUID,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    kind: Literal["lines", "zones"] = "lines",
    bucket: Literal["minute", "hour", "day"] = "hour",
    delimiter: Literal[",", ";"] = ",",
    ctx: OrgContext = Depends(org_access()),
    db: Session = Depends(get_db),
) -> Response:
    site = sites.get_site(db, ctx.org, site_id)
    p = _period(site, from_, to)
    make = exports.lines_csv if kind == "lines" else exports.zones_csv
    text = make(db, site, p, bucket, delimiter)
    return _download(text.encode("utf-8"), "text/csv; charset=utf-8", exports.filename(site, p, kind, "csv"))


@router.get("/sites/{site_id}/report.pdf")
def export_pdf(
    site_id: uuid.UUID,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    ctx: OrgContext = Depends(org_access()),
    db: Session = Depends(get_db),
) -> Response:
    site = sites.get_site(db, ctx.org, site_id)
    p = _period(site, from_, to)
    data = pdf.build_pdf(analytics.dashboard(db, site, p), ctx.org.name)
    return _download(data, "application/pdf", exports.filename(site, p, "report", "pdf"))


@router.post("/sample-data", status_code=201)
def add_sample_data(
    ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Add the "Demo café (sample data)" site with 5 weeks of invented numbers (admins)."""
    if not get_settings().sample_data_enabled:
        raise AppError("Sample data is switched off on this server.", code="disabled", status=403)
    if any(s.name == sample_data.SITE_NAME for s, _, _ in sites.list_sites(db, ctx.org)):
        raise AppError("The sample site already exists.", code="exists", status=409)
    site = sample_data.create(db, ctx.org)
    audit.record(
        db,
        "site.sample_data_added",
        actor=ctx.user.id,
        org_id=ctx.org.id,
        target_type="site",
        target_id=site.id,
    )
    db.commit()
    return {"site_id": site.id, "name": site.name}


@router.delete("/sample-data", response_model=OkOut)
def remove_sample_data(
    ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> OkOut:
    """Remove the sample site, its sample device and all its invented numbers."""
    if not sample_data.remove(db, ctx.org):
        raise AppError("There is no sample site.", code="not_found", status=404)
    audit.record(db, "site.sample_data_removed", actor=ctx.user.id, org_id=ctx.org.id)
    db.commit()
    return OkOut(message="Sample data removed.")


# --- report schedules -------------------------------------------------------------------------


def _schedule_out(db: Session, s: ReportSchedule) -> ReportScheduleOut:
    members = reports._members(db, s.org_id)
    now = utcnow()
    nxt = reports.next_send(s.frequency, s.weekday, s.hour, s.site.timezone, now) if s.enabled else None
    nxt_period = None
    if nxt is not None:
        d = reports.latest_send(s.frequency, s.weekday, s.hour, s.site.timezone, nxt)
        nxt_period = {"from": d.first.isoformat(), "to": d.last.isoformat()}
    return ReportScheduleOut(
        id=s.id,
        site_id=s.site_id,
        site_name=s.site.name,
        timezone=s.site.timezone,
        frequency=s.frequency,
        weekday=s.weekday,
        hour=s.hour,
        attach_csv=s.attach_csv,
        enabled=s.enabled,
        created_at=s.created_at,
        recipients=[
            ReportRecipientOut(user_id=u.id, name=u.name, email=u.email, email_verified=u.email_verified)
            for r in s.recipients
            if (u := members.get(r))
        ],
        last_sent_at=s.last_sent_at,
        last_period_end=s.last_period_end.isoformat() if s.last_period_end else None,
        last_error=s.last_error,
        next_send_at=nxt,
        next_period=nxt_period,
    )


@router.get("/report-schedules", response_model=list[ReportScheduleOut])
def list_schedules(
    ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)
) -> list[ReportScheduleOut]:
    return [_schedule_out(db, s) for s in reports.list_schedules(db, ctx.org)]


@router.post("/report-schedules", response_model=ReportScheduleOut, status_code=201)
def create_schedule(
    body: ReportScheduleIn, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> ReportScheduleOut:
    site = sites.get_site(db, ctx.org, body.site_id)
    s = reports.create_schedule(
        db,
        ctx.org,
        ctx.user,
        site,
        frequency=body.frequency,
        weekday=body.weekday,
        hour=body.hour,
        recipients=body.recipients,
        attach_csv=body.attach_csv,
    )
    db.commit()
    return _schedule_out(db, s)


@router.patch("/report-schedules/{schedule_id}", response_model=ReportScheduleOut)
def update_schedule(
    schedule_id: uuid.UUID,
    body: ReportSchedulePatch,
    ctx: OrgContext = Depends(org_access(Role.MEMBER)),
    db: Session = Depends(get_db),
) -> ReportScheduleOut:
    s = reports.get_schedule(db, ctx.org, schedule_id)
    site = sites.get_site(db, ctx.org, body.site_id) if body.site_id else None
    reports.update_schedule(db, ctx.org, ctx.user, s, body.model_dump(exclude_unset=True), site)
    db.commit()
    return _schedule_out(db, s)


@router.delete("/report-schedules/{schedule_id}", response_model=OkOut)
def delete_schedule(
    schedule_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> OkOut:
    reports.delete_schedule(db, ctx.org, ctx.user, reports.get_schedule(db, ctx.org, schedule_id))
    db.commit()
    return OkOut(message="Report deleted.")


@router.post("/report-schedules/{schedule_id}/send", response_model=SendNowOut)
def send_now(
    schedule_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> SendNowOut:
    s = reports.get_schedule(db, ctx.org, schedule_id)
    n, due = reports.send_now(db, ctx.org, ctx.user, s)
    db.commit()
    return SendNowOut(emails=n, period_from=due.first.isoformat(), period_to=due.last.isoformat())
