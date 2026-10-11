"""Report schedules: a PDF report of a site, emailed daily, weekly or monthly to members.

When is a report due? (all in the site's time zone)
- daily:   every day at ``hour``, about yesterday.
- weekly:  every ``weekday`` at ``hour``, about the 7 days before that day
           (weekday 0 = Monday -> last Monday to Sunday).
- monthly: on the 1st at ``hour``, about the last month.

Celery beat checks every 5 minutes. ``last_period_end`` stores the last day that was sent, so a
report goes out once, and after a long pause (server down for 3 days) only the newest missed
report is sent, never a flood. A new schedule waits for its first send time.

Recipients are members of the organization with a confirmed email address (stored as user ids,
so somebody who leaves stops getting reports). One email per person: nobody sees the others.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError, NotFound
from ...saas.models import Membership, Organization, User
from ...saas.services import audit, email, ratelimit
from ...settings import get_settings
from ..models import ReportSchedule, Site
from ..reports import pdf
from . import analytics, exports

log = logging.getLogger(__name__)

FREQUENCIES = ("daily", "weekly", "monthly")
MAX_SCHEDULES = 50
MAX_FAILURES = 3  # after 3 failed tries for the same period, skip that period (no endless retries)
SEND_NOW_PER_HOUR = 10


# --------------------------------------------------------------------------- when


@dataclass(frozen=True)
class Due:
    send_at: datetime  # local send time of the newest report (aware)
    first: date
    last: date


def latest_send(frequency: str, weekday: int, hour: int, tz: str, now: datetime) -> Due:
    """The newest send time that is not in the future, and the period that report covers."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    today = local.date()

    def at(day: date) -> datetime:
        return datetime.combine(day, time(hour), tzinfo=zone)

    if frequency == "daily":
        day = today if at(today) <= local else today - timedelta(days=1)
        return Due(at(day), day - timedelta(days=1), day - timedelta(days=1))
    if frequency == "weekly":
        back = (today.weekday() - weekday) % 7
        day = today - timedelta(days=back)
        if at(day) > local:
            day -= timedelta(days=7)
        return Due(at(day), day - timedelta(days=7), day - timedelta(days=1))
    if frequency == "monthly":
        day = today.replace(day=1)
        if at(day) > local:
            day = (day - timedelta(days=1)).replace(day=1)
        last = day - timedelta(days=1)
        return Due(at(day), last.replace(day=1), last)
    raise ValueError(f"unknown frequency {frequency}")


def next_send(frequency: str, weekday: int, hour: int, tz: str, now: datetime) -> datetime:
    """The next send time after ``now`` (for the web page)."""
    probe = now
    for _ in range(40):
        due = latest_send(frequency, weekday, hour, tz, probe)
        if due.send_at > now:
            return due.send_at
        probe += timedelta(days=1)
    return latest_send(frequency, weekday, hour, tz, probe).send_at


def is_due(s: ReportSchedule, now: datetime) -> Due | None:
    if not s.enabled:
        return None
    due = latest_send(s.frequency, s.weekday, s.hour, s.site.timezone, now)
    if due.send_at < s.created_at:
        return None  # created after that send time: wait for the next one
    if s.last_period_end is not None and s.last_period_end >= due.last:
        return None
    return due


def period_label(frequency: str) -> str:
    return {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}[frequency]


# --------------------------------------------------------------------------- CRUD


def _members(db: Session, org_id: uuid.UUID) -> dict[str, User]:
    rows = db.scalars(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == org_id, User.is_active.is_(True))
    )
    return {str(u.id): u for u in rows}


def _check_recipients(db: Session, org: Organization, recipients: list[uuid.UUID]) -> list[str]:
    members = _members(db, org.id)
    ids = list(dict.fromkeys(str(r) for r in recipients))
    unknown = [r for r in ids if r not in members]
    if unknown:
        raise AppError(
            "Reports can only go to members of this organization.",
            code="bad_recipient",
            status=422,
            fields={"recipients": "Choose members of this organization."},
        )
    if not ids:
        raise AppError(
            "Choose at least one person.",
            code="no_recipients",
            status=422,
            fields={"recipients": "Choose at least one person."},
        )
    return ids


def list_schedules(db: Session, org: Organization) -> list[ReportSchedule]:
    return list(
        db.scalars(
            select(ReportSchedule).where(ReportSchedule.org_id == org.id).order_by(ReportSchedule.created_at)
        )
    )


def get_schedule(db: Session, org: Organization, schedule_id: uuid.UUID) -> ReportSchedule:
    s = db.scalar(
        select(ReportSchedule).where(ReportSchedule.org_id == org.id, ReportSchedule.id == schedule_id)
    )
    if s is None:
        raise NotFound("Report not found.")
    return s


def create_schedule(
    db: Session,
    org: Organization,
    actor: User,
    site: Site,
    *,
    frequency: str,
    weekday: int,
    hour: int,
    recipients: list[uuid.UUID],
    attach_csv: bool,
) -> ReportSchedule:
    if len(list_schedules(db, org)) >= MAX_SCHEDULES:
        raise AppError(f"At most {MAX_SCHEDULES} reports per organization.", code="limit", status=409)
    s = ReportSchedule(
        org_id=org.id,
        site_id=site.id,
        frequency=frequency,
        weekday=weekday,
        hour=hour,
        recipients=_check_recipients(db, org, recipients),
        attach_csv=attach_csv,
        created_by=actor.id,
        created_at=utcnow(),
    )
    db.add(s)
    db.flush()
    db.refresh(s)
    audit.record(
        db,
        "report_schedule.created",
        actor=actor.id,
        org_id=org.id,
        target_type="report_schedule",
        target_id=s.id,
        site=site.name,
        frequency=frequency,
        recipients=len(s.recipients),
    )
    return s


def update_schedule(
    db: Session, org: Organization, actor: User, s: ReportSchedule, changes: dict[str, Any], site: Site | None
) -> ReportSchedule:
    if site is not None:
        s.site_id = site.id
        s.site = site
    for key in ("frequency", "weekday", "hour", "attach_csv", "enabled"):
        if changes.get(key) is not None:
            setattr(s, key, changes[key])
    if changes.get("recipients") is not None:
        s.recipients = _check_recipients(db, org, changes["recipients"])
    if (
        any(changes.get(k) is not None for k in ("frequency", "weekday", "hour", "site_id"))
        or changes.get("enabled") is True
    ):
        # a changed timing starts fresh: no report for a time before this change
        s.created_at = utcnow()
        s.failures = 0
        s.last_error = None
    audit.record(
        db,
        "report_schedule.updated",
        actor=actor.id,
        org_id=org.id,
        target_type="report_schedule",
        target_id=s.id,
        fields=sorted(k for k, v in changes.items() if v is not None),
    )
    return s


def delete_schedule(db: Session, org: Organization, actor: User, s: ReportSchedule) -> None:
    audit.record(
        db,
        "report_schedule.deleted",
        actor=actor.id,
        org_id=org.id,
        target_type="report_schedule",
        target_id=s.id,
        frequency=s.frequency,
    )
    db.delete(s)
    db.flush()


def recipients_of(db: Session, s: ReportSchedule) -> list[User]:
    """Current members among the recipients, with a confirmed email address."""
    members = _members(db, s.org_id)
    return [members[r] for r in s.recipients if r in members and members[r].email_verified]


# --------------------------------------------------------------------------- sending


def build_files(
    db: Session, s: ReportSchedule, first: date, last: date, now: datetime | None = None
) -> tuple[dict[str, Any], list[email.Attachment]]:
    org = db.get(Organization, s.org_id)
    period = analytics.Period(first, last, s.site.timezone)
    dash = analytics.dashboard(db, s.site, period, now)
    files = [
        email.Attachment.of(
            exports.filename(s.site, period, "report", "pdf"),
            "application/pdf",
            pdf.build_pdf(dash, org.name if org else "", now),
        )
    ]
    if s.attach_csv:
        csv_text = exports.lines_csv(db, s.site, period, "hour" if period.days <= 31 else "day", ";", now)
        files.append(
            email.Attachment.of(
                exports.filename(s.site, period, "lines", "csv"), "text/csv", csv_text.encode("utf-8")
            )
        )
    return dash, files


def report_mail(
    to: str, s: ReportSchedule, dash: dict[str, Any], files: list[email.Attachment], org_name: str
) -> email.Mail:
    t = dash["totals"]
    days = dash["period"]["days"]
    change = t["change_pct"]
    vs = "" if change is None else f" ({change:+.0f} % vs {dash['compare']['label']})"
    lines = [f"Visitors in: {pdf.num(t['in'])}{vs}."]
    if dash["peak"]:
        lines.append(
            f"Busiest hour: {pdf.peak_text(dash['peak'], days)} with {pdf.num(dash['peak']['in'])} visitors."
        )
    if dash["dwell"]["count"]:
        lines.append(f"Average visit in zones: {pdf.duration(dash['dwell']['avg_s'])}.")
    cov = dash["coverage"]["pct"]
    if cov is not None and cov < 90:
        lines.append(
            f"Note: the cameras ran only {cov:.0f} % of the expected time, so the real numbers are higher."
        )
    if not dash["has_data"]:
        lines = ["There was no counting data in this period. Is the device online?"]
    lines.append("The full report is attached as a PDF.")
    url = f"{get_settings().web_url}/app/orgs/{s.org_id}/sites/{s.site_id}"
    label = period_label(s.frequency).lower()
    mail = email.build(
        to,
        f"{s.site.name}: {label} report, {pdf.period_text(dash)}",
        f"{s.site.name}, {pdf.period_text(dash)}",
        lines,
        ("Open the dashboard", url),
        f"You get this {label} report as a member of {org_name} on CountVision. "
        "Change or stop it on the Reports page.",
    )
    return replace(mail, attachments=tuple(files))


def send(db: Session, s: ReportSchedule, first: date, last: date, now: datetime | None = None) -> int:
    """Queue the report for every recipient (sent after the commit). Returns the number of emails."""
    people = recipients_of(db, s)
    if not people:
        return 0
    org = db.get(Organization, s.org_id)
    dash, files = build_files(db, s, first, last, now)
    for user in people:
        email.queue(db, report_mail(user.email, s, dash, files, org.name if org else ""))
    return len(people)


def send_now(db: Session, org: Organization, actor: User, s: ReportSchedule) -> tuple[int, Due]:
    """'Send now' button: the newest finished period, at once. Does not change the timer."""
    ratelimit.limit(
        "report_now", str(org.id), SEND_NOW_PER_HOUR, 3600, "Too many test reports. Please wait a while."
    )
    due = latest_send(s.frequency, s.weekday, s.hour, s.site.timezone, utcnow())
    n = send(db, s, due.first, due.last)
    if n == 0:
        raise AppError(
            "Nobody on the list can get emails yet (members must confirm their email address).",
            code="no_recipients",
            status=409,
        )
    audit.record(
        db,
        "report.sent_now",
        actor=actor.id,
        org_id=org.id,
        target_type="report_schedule",
        target_id=s.id,
        emails=n,
        period=f"{due.first}..{due.last}",
    )
    return n, due


def send_due(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Called by Celery beat: send every report that is due. Each schedule in its own transaction."""
    now = now or utcnow()
    result = {"sent": 0, "emails": 0, "failed": 0}
    ids = list(db.scalars(select(ReportSchedule.id).where(ReportSchedule.enabled.is_(True))))
    db.rollback()
    for sid in ids:
        # lock the row: two workers never send the same report
        s = db.scalar(
            select(ReportSchedule).where(ReportSchedule.id == sid).with_for_update(skip_locked=True)
        )
        if s is None:
            db.rollback()
            continue
        due = is_due(s, now)
        if due is None:
            db.rollback()
            continue
        try:
            with db.begin_nested():
                n = send(db, s, due.first, due.last, now)
            s.last_period_end = due.last
            s.last_sent_at = now
            s.last_error = None if n else "Nobody on the list has a confirmed email address."
            s.failures = 0
            result["sent"] += 1
            result["emails"] += n
            log.info("Report %s (%s) for %s..%s sent to %d people", s.id, s.frequency, due.first, due.last, n)
        except Exception as exc:  # one broken report must not stop the others
            log.exception("Report %s failed", s.id)
            s.failures += 1
            s.last_error = f"{type(exc).__name__}: {exc}"[:300]
            if s.failures >= MAX_FAILURES:
                s.last_period_end = due.last  # give up on this period, try the next one
                s.failures = 0
            result["failed"] += 1
        db.commit()  # emails go out after this commit
    return result
