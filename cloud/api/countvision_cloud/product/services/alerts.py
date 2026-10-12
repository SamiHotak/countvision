"""Alert rules: checked every minute by Celery beat (``evaluate_all``).

Kinds (all per site, optionally one camera / one zone):
- occupancy: people in area zones (summed) >= threshold in each of the last ``duration_min``
  analysed minutes (1-minute averages from zone_stats). Ends when 2 minutes in a row are below.
- queue: the same with the queue length of queue zones.
- camera_offline: a camera does not count for ``duration_min`` minutes: its device has not
  reported for 90 s, or the camera itself is disconnected / failed. Outside the camera's counting
  hours nothing is checked (a shop computer may be switched off at night).
- traffic_drop: visitors (IN on the site's visitor lines) in the last ``duration_min`` minutes are
  below ``threshold`` % of a normal week (the same weekday and time in the last 4 weeks, only weeks
  where the cameras ran). Needs at least 2 such weeks and at least 10 normal visitors, and the
  cameras must have run now too (otherwise the camera_offline rule is the right one). Checked every
  5 minutes.

Only numbers from the database are used (minute rows arrive about 1-2 minutes after the minute),
so an alert comes 1-3 minutes after the moment it became true.
A firing rule sends one message, and one more when it is over ("resolved").
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit
from ...settings import get_settings
from ..models import AlertEvent, AlertRule, Camera, Coverage, Device, NotificationChannel, Site, ZoneStat
from . import analytics, camera_config, devices, notify

log = logging.getLogger(__name__)

KINDS = ("occupancy", "queue", "camera_offline", "traffic_drop")
FRESH_S = 4 * 60  # the newest zone minute must be at most this old to say anything
DROP_EVERY_S = 5 * 60
DROP_WEEKS = 4
DROP_MIN_TYPICAL = 10.0
DROP_MIN_COVERAGE = 0.8
MAX_RULES = 100
KIND_LABEL = {
    "occupancy": "Too many people",
    "queue": "Long queue",
    "camera_offline": "Camera not counting",
    "traffic_drop": "Fewer visitors than usual",
}


# --------------------------------------------------------------------------- conditions


class Check:
    """Result of one evaluation: is the condition true, the value, the words for the message."""

    def __init__(
        self,
        true: bool | None,
        value: float | None,
        text: str,
        since: datetime | None = None,
        clear: bool | None = None,
    ) -> None:
        self.true = true  # None = cannot say (no data): keep the current state
        self.value = value
        self.text = text
        self.since = since  # when it became true (camera_offline)
        self.clear = (true is False) if clear is None else clear  # strong enough to resolve


def _zone_names(db: Session, rule: AlertRule, kind: str) -> list[tuple[uuid.UUID, str]]:
    sc = analytics.site_cameras(db, rule.site)
    want = "queue" if kind == "queue" else "area"
    out = [
        (cam, zone)
        for (cam, zone), k in sc.zone_kinds.items()
        if k == want
        and (rule.camera_id is None or cam == rule.camera_id)
        and (rule.zone is None or zone == rule.zone)
    ]
    return out


def check_zone(db: Session, rule: AlertRule, now: datetime) -> Check:
    zones = _zone_names(db, rule, rule.kind)
    label = "people waiting" if rule.kind == "queue" else "people inside"
    if not zones:
        return Check(None, None, "No matching zone on this site (draw one in the camera editor).")
    n = max(1, rule.duration_min)
    cams = list({c for c, _ in zones})
    names = {z for _, z in zones}
    col = ZoneStat.queue_avg if rule.kind == "queue" else ZoneStat.occ_avg
    rows = db.execute(
        select(ZoneStat.window_start, ZoneStat.camera_id, ZoneStat.zone, col).where(
            ZoneStat.camera_id.in_(cams),
            ZoneStat.zone.in_(names),
            ZoneStat.class_name == "*",
            ZoneStat.window_start >= now - timedelta(minutes=n + 6),
        )
    ).all()
    per_min: dict[datetime, float] = {}
    for ws, cam, zone, v in rows:
        if (cam, zone) in zones:
            per_min[ws] = per_min.get(ws, 0.0) + float(v or 0)
    if not per_min:
        return Check(None, None, f"No zone numbers in the last {n + 6} minutes.")
    minutes = sorted(per_min, reverse=True)
    newest = minutes[0]
    if (now - (newest + timedelta(seconds=60))).total_seconds() > FRESH_S:
        return Check(None, per_min[newest], "Zone numbers are older than 4 minutes.")
    value = round(per_min[newest], 1)
    last = minutes[:n]
    contiguous = len(last) == n and all(
        (last[i] - last[i + 1]) == timedelta(seconds=60) for i in range(len(last) - 1)
    )
    true = contiguous and all(per_min[m] >= rule.threshold for m in last)
    clear = len(minutes) >= 2 and all(per_min[m] < rule.threshold for m in minutes[:2])
    where = rule.zone or ("all queue zones" if rule.kind == "queue" else "all area zones")
    text = f"{value:g} {label} in {where} (limit {rule.threshold:g}"
    text += f", for {n} min)." if true else ")."
    return Check(true, value, text, clear=clear)


def in_schedule(schedule, tz: str, now: datetime) -> bool:
    """Is ``now`` inside the camera's counting hours? No schedule = always."""
    if schedule is None:
        return True
    return analytics.expected_seconds(now - timedelta(seconds=1), now, tz, schedule) > 0


def check_offline(db: Session, rule: AlertRule, now: datetime) -> Check:
    site = rule.site
    stmt = (
        select(Camera, Device)
        .join(Device, Device.id == Camera.device_id)
        .where(Device.site_id == site.id, Device.revoked_at.is_(None))
    )
    if rule.camera_id:
        stmt = stmt.where(Camera.id == rule.camera_id)
    bad: list[tuple[str, datetime | None]] = []
    total = 0
    for cam, dev in db.execute(stmt).all():
        doc, _ = camera_config.effective_config(cam)
        if not in_schedule(doc.schedule if doc else None, site.timezone, now):
            continue
        total += 1
        if not devices.is_online(dev, now):
            bad.append((f"{cam.name} (computer {dev.name} offline)", dev.last_seen_at))
        elif cam.state not in ("running", "paused") or cam.connected is False:
            bad.append((f"{cam.name} ({cam.state or 'no state'})", None))
    if total == 0:
        return Check(False, 0, "No camera has to count right now.")
    if not bad:
        return Check(False, 0, f"All {total} cameras are counting.")
    since = [s for _, s in bad if s is not None]
    started = min(since) if len(since) == len(bad) else None  # None = use the rule's own clock
    names = ", ".join(n for n, _ in bad)
    return Check(True, float(len(bad)), f"Not counting: {names}.", since=started)


def visitors_between(
    db: Session, site: Site, sc: analytics.SiteCameras, start: datetime, end: datetime
) -> int:
    raw = analytics.line_hours(db, sc.ids, start, end, site.timezone, unit="minute")
    sel = analytics.visitor_filter(site)
    return sum(
        v[0]
        for (cam, line, cls, _), v in raw.items()
        if cls == analytics.ALL and analytics.is_visitor(sel, cam, line)
    )


def coverage_share(db: Session, ids: list[uuid.UUID], start: datetime, end: datetime) -> float:
    if not ids or end <= start:
        return 0.0
    secs = (
        db.scalar(
            select(func.coalesce(func.sum(Coverage.seconds), 0)).where(
                Coverage.camera_id.in_(ids), Coverage.window_start >= start, Coverage.window_start < end
            )
        )
        or 0
    )
    return float(secs) / ((end - start).total_seconds() * max(1, len(ids)))


def check_drop(db: Session, rule: AlertRule, now: datetime) -> Check:
    site = rule.site
    sc = analytics.site_cameras(db, site)
    ids = [rule.camera_id] if rule.camera_id else sc.ids
    window = timedelta(minutes=max(30, rule.duration_min))
    # minute rows arrive late: look at the window that ended 2 minutes ago
    end = (now - timedelta(minutes=2)).replace(second=0, microsecond=0)
    start = end - window
    for cam in sc.cameras:
        if cam.id in ids and not in_schedule(sc.schedules.get(cam.id), site.timezone, start):
            return Check(None, None, "Outside the counting hours.")
    if coverage_share(db, ids, start, end) < DROP_MIN_COVERAGE:
        return Check(None, None, "The cameras did not run the whole time (see the camera alert).")
    current = visitors_between(db, site, sc, start, end)
    weeks = []
    for w in range(1, DROP_WEEKS + 1):
        s, e = start - timedelta(weeks=w), end - timedelta(weeks=w)
        if coverage_share(db, ids, s, e) >= DROP_MIN_COVERAGE:
            weeks.append(visitors_between(db, site, sc, s, e))
    if len(weeks) < 2:
        return Check(None, None, "Not enough history yet (needs 2 normal weeks).")
    typical = sum(weeks) / len(weeks)
    if typical < DROP_MIN_TYPICAL:
        return Check(False, None, f"Normally only {typical:.0f} visitors at this time: too few to compare.")
    pct = round(current / typical * 100, 1)
    minutes = int(window.total_seconds() // 60)
    text = (
        f"{current} visitors in the last {minutes} min, normally {typical:.0f} "
        f"({pct:.0f} %, limit {rule.threshold:g} %)."
    )
    return Check(pct < rule.threshold, pct, text)


def check(db: Session, rule: AlertRule, now: datetime) -> Check:
    if rule.kind in ("occupancy", "queue"):
        return check_zone(db, rule, now)
    if rule.kind == "camera_offline":
        return check_offline(db, rule, now)
    return check_drop(db, rule, now)


# --------------------------------------------------------------------------- state machine


def _url(rule: AlertRule) -> str:
    return f"{get_settings().web_url}/app/orgs/{rule.org_id}/sites/{rule.site_id}"


def _message(rule: AlertRule, status: str, chk: Check, now: datetime) -> notify.Message:
    site = rule.site
    local = now.astimezone(ZoneInfo(site.timezone))
    title = f"🔴 {site.name}: {rule.name}" if status == "fired" else f"✅ {site.name}: {rule.name} is over"
    text = f"{chk.text} {local:%H:%M}, {local:%d.%m.%Y}."
    data = {
        "rule_id": str(rule.id),
        "rule_name": rule.name,
        "kind": rule.kind,
        "status": status,
        "site_id": str(site.id),
        "site_name": site.name,
        "value": chk.value,
        "threshold": rule.threshold,
        "duration_min": rule.duration_min,
        "message": chk.text,
        "at": now.isoformat(),
    }
    return notify.Message(title=title, text=text, event_type=f"alert.{status}", data=data, url=_url(rule))


def _channels(db: Session, rule: AlertRule) -> list[NotificationChannel]:
    ids = [uuid.UUID(c) for c in rule.channel_ids or []]
    if not ids:
        return []
    return list(
        db.scalars(
            select(NotificationChannel).where(
                NotificationChannel.org_id == rule.org_id, NotificationChannel.id.in_(ids)
            )
        )
    )


def _record(db: Session, rule: AlertRule, status: str, chk: Check, now: datetime) -> AlertEvent:
    ev = AlertEvent(
        org_id=rule.org_id,
        rule_id=rule.id,
        site_id=rule.site_id,
        rule_name=rule.name,
        kind=rule.kind,
        status=status,
        value=chk.value,
        message=chk.text[:500],
        created_at=now,
    )
    db.add(ev)
    msg = _message(rule, status, chk, now)
    for ch in _channels(db, rule):
        notify.send(db, ch, msg)
    from .. import live

    live.queue(
        db,
        rule.org_id,
        {
            "type": "alert",
            "rule_id": str(rule.id),
            "status": status,
            "site_id": str(rule.site_id),
            "message": chk.text,
        },
    )
    log.info("Alert %s %s: %s", rule.id, status, chk.text)
    return ev


def step(db: Session, rule: AlertRule, now: datetime) -> str | None:
    """Evaluate one rule and move its state. Returns 'fired', 'resolved' or None."""
    if (
        rule.kind == "traffic_drop"
        and rule.last_eval_at
        and (now - rule.last_eval_at).total_seconds() < DROP_EVERY_S - 5
    ):
        return None
    chk = check(db, rule, now)
    rule.last_eval_at = now
    rule.last_value = chk.value
    rule.last_message = chk.text[:300]
    if chk.true is None:
        return None
    if chk.true:
        rule.last_true_at = now
        if rule.true_since is None:
            rule.true_since = chk.since or now
        elif chk.since is not None and chk.since < rule.true_since:
            rule.true_since = chk.since
        held = (now - rule.true_since).total_seconds() / 60
        need = rule.duration_min if rule.kind == "camera_offline" else 0  # zone/drop: the data has the time
        if rule.state != "firing" and held + 1e-9 >= need:
            rule.state, rule.fired_at = "firing", now
            _record(db, rule, "fired", chk, now)
            return "fired"
        return None
    rule.true_since = None
    if rule.state == "firing" and chk.clear:
        rule.state = "ok"
        _record(db, rule, "resolved", chk, now)
        return "resolved"
    return None


def evaluate_all(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Celery beat, every minute. Each rule in its own small transaction (row lock, skip busy)."""
    now = now or utcnow()
    result = {"checked": 0, "fired": 0, "resolved": 0, "errors": 0}
    ids = list(db.scalars(select(AlertRule.id).where(AlertRule.enabled.is_(True))))
    db.rollback()
    for rid in ids:
        rule = db.scalar(select(AlertRule).where(AlertRule.id == rid).with_for_update(skip_locked=True))
        if rule is None:
            db.rollback()
            continue
        try:
            with db.begin_nested():
                outcome = step(db, rule, now)
            result["checked"] += 1
            if outcome:
                result[outcome] += 1
        except Exception:  # one broken rule must not stop the others
            log.exception("Alert rule %s failed", rid)
            result["errors"] += 1
        db.commit()
    return result


# --------------------------------------------------------------------------- CRUD


def _validate(db: Session, org: Organization, data: dict[str, Any], site: Site) -> None:
    kind = data["kind"]
    if kind not in KINDS:
        raise AppError("Unknown alert kind.", code="bad_kind", status=422)
    if data.get("camera_id"):
        cam = db.scalar(
            select(Camera)
            .join(Device, Device.id == Camera.device_id)
            .where(Camera.id == data["camera_id"], Device.site_id == site.id)
        )
        if cam is None:
            raise AppError(
                "This camera is not on the chosen site.",
                code="bad_camera",
                status=422,
                fields={"camera_id": "Choose a camera of this site."},
            )
    if kind in ("occupancy", "queue") and data.get("threshold", 0) <= 0:
        raise AppError(
            "Set a number of people above 0.",
            code="bad_threshold",
            status=422,
            fields={"threshold": "Must be above 0."},
        )
    if kind == "traffic_drop" and not (0 < data.get("threshold", 0) < 100):
        raise AppError(
            "Set a percentage between 1 and 99.",
            code="bad_threshold",
            status=422,
            fields={"threshold": "Between 1 and 99 %."},
        )
    ids = [str(c) for c in data.get("channel_ids") or []]
    found = (
        {
            str(c)
            for c in db.scalars(
                select(NotificationChannel.id).where(
                    NotificationChannel.org_id == org.id,
                    NotificationChannel.id.in_([uuid.UUID(i) for i in ids]),
                )
            )
        }
        if ids
        else set()
    )
    if set(ids) - found:
        raise AppError("Unknown channel.", code="bad_channel", status=422)
    if not ids:
        raise AppError(
            "Choose where the alert goes (at least one channel).",
            code="no_channel",
            status=422,
            fields={"channel_ids": "Choose at least one channel."},
        )


def get_rule(db: Session, org: Organization, rule_id: uuid.UUID) -> AlertRule:
    rule = db.scalar(select(AlertRule).where(AlertRule.org_id == org.id, AlertRule.id == rule_id))
    if rule is None:
        raise NotFound("Alert not found.")
    return rule


def create_rule(db: Session, org: Organization, actor: User, site: Site, data: dict[str, Any]) -> AlertRule:
    if (db.scalar(select(func.count(AlertRule.id)).where(AlertRule.org_id == org.id)) or 0) >= MAX_RULES:
        raise AppError(f"At most {MAX_RULES} alerts per organization.", code="limit", status=409)
    _validate(db, org, data, site)
    rule = AlertRule(
        org_id=org.id,
        site_id=site.id,
        name=data.get("name") or KIND_LABEL[data["kind"]],
        kind=data["kind"],
        camera_id=data.get("camera_id"),
        zone=data.get("zone") or None,
        threshold=data.get("threshold") or 0,
        duration_min=data.get("duration_min") or 5,
        channel_ids=[str(c) for c in data["channel_ids"]],
        created_by=actor.id,
    )
    rule.site = site
    db.add(rule)
    db.flush()
    audit.record(
        db,
        "alert_rule.created",
        actor=actor.id,
        org_id=org.id,
        target_type="alert_rule",
        target_id=rule.id,
        kind=rule.kind,
        site=site.name,
    )
    return rule


def update_rule(
    db: Session, org: Organization, actor: User, rule: AlertRule, data: dict[str, Any], site: Site | None
) -> AlertRule:
    merged = {
        "kind": rule.kind,
        "camera_id": rule.camera_id,
        "threshold": rule.threshold,
        "channel_ids": rule.channel_ids,
        **{k: v for k, v in data.items() if v is not None},
    }
    if "camera_id" in data and data["camera_id"] is None:
        merged["camera_id"] = None
    target = site or rule.site
    _validate(db, org, merged, target)
    for key in ("name", "kind", "threshold", "duration_min", "enabled"):
        if data.get(key) is not None:
            setattr(rule, key, data[key])
    if "zone" in data:
        rule.zone = data["zone"] or None
    if "camera_id" in data:
        rule.camera_id = data["camera_id"]
    if data.get("channel_ids") is not None:
        rule.channel_ids = [str(c) for c in data["channel_ids"]]
    if site is not None:
        rule.site_id, rule.site = site.id, site
    if any(k in data for k in ("kind", "threshold", "duration_min", "zone", "camera_id", "site_id")):
        rule.true_since = None  # a changed condition starts fresh (a firing alert stays until clear)
    audit.record(
        db,
        "alert_rule.updated",
        actor=actor.id,
        org_id=org.id,
        target_type="alert_rule",
        target_id=rule.id,
        fields=sorted(k for k, v in data.items() if v is not None),
    )
    return rule


def delete_rule(db: Session, org: Organization, actor: User, rule: AlertRule) -> None:
    audit.record(
        db,
        "alert_rule.deleted",
        actor=actor.id,
        org_id=org.id,
        target_type="alert_rule",
        target_id=rule.id,
        name=rule.name,
    )
    db.delete(rule)
    db.flush()


def history(
    db: Session, org: Organization, limit: int = 100, since: datetime | None = None
) -> list[AlertEvent]:
    stmt = select(AlertEvent).where(AlertEvent.org_id == org.id)
    if since is not None:
        stmt = stmt.where(AlertEvent.created_at >= since)
    return list(db.scalars(stmt.order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc()).limit(limit)))
