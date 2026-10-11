"""Dashboard numbers for a site (Phase 4 A).

Everything is computed in the site's time zone ("today", hours, weekdays), from numbers only:

- line counts: the 1-minute rows (``line_counts``) for finished minutes, plus the single crossing
  events (``count_events``) for the newest minutes that are not aggregated yet. The border per
  camera is the end of its newest ``coverage`` minute (the edge writes coverage and line counts of
  a minute together). So the dashboard is exact AND live: a crossing shows within seconds.
- zones: ``zone_stats`` (class ``*`` = all classes), occupancy weighted by analysed seconds.
- dwell times: ``zone_visit`` events.
- coverage: how many seconds were analysed vs. how many were expected (inside counting hours).
  A low coverage means the numbers are incomplete, and the dashboard says so.

"Visitors" are the lines selected on the site (``Site.visitor_lines``); by default every line.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, literal, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError
from ..models import Camera, CountEvent, Coverage, Device, LineCount, Site, ZoneStat
from . import camera_config

log = logging.getLogger(__name__)

MAX_DAYS = 366
HEATMAP_DAYS = 28
DWELL_EDGES = [30, 60, 120, 300, 600, 1200]  # seconds; buckets <30 s, 30-60 s, 1-2 min, ...
DWELL_LABELS = ["< 30 s", "30-60 s", "1-2 min", "2-5 min", "5-10 min", "10-20 min", "> 20 min"]
OCCUPANCY_FRESH_S = 5 * 60  # a zone value older than this is not "now" any more
ALL = "*"


# --------------------------------------------------------------------------- periods


@dataclass(frozen=True)
class Period:
    """Local dates [first, last] (both included) in a time zone."""

    first: date
    last: date
    tz: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    @property
    def start(self) -> datetime:
        return local_start(self.first, self.tz)

    @property
    def end(self) -> datetime:
        return local_start(self.last + timedelta(days=1), self.tz)

    @property
    def bucket(self) -> str:
        """Hours for 1-2 days, days for longer periods."""
        return "hour" if self.days <= 2 else "day"


def local_start(day: date, tz: str) -> datetime:
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(tz))


def local_today(tz: str, now: datetime | None = None) -> date:
    return (now or utcnow()).astimezone(ZoneInfo(tz)).date()


def parse_period(tz: str, first: date | None, last: date | None, now: datetime | None = None) -> Period:
    """The period from query parameters (default: today). Raises a 422 error for bad input."""
    today = local_today(tz, now)
    first = first or last or today
    last = last or first
    if last < first:
        raise AppError(
            "The end date is before the start date.",
            code="bad_period",
            status=422,
            fields={"to": "Must be on or after the start date."},
        )
    if (last - first).days + 1 > MAX_DAYS:
        raise AppError(f"Choose at most {MAX_DAYS} days.", code="bad_period", status=422)
    if first > today:
        raise AppError("The period starts in the future.", code="bad_period", status=422)
    return Period(first, last, tz)


@dataclass(frozen=True)
class Comparison:
    """The period we compare with: last week for up to 7 days, else the period before."""

    shift_days: int
    start: datetime
    end: datetime
    first: date
    last: date
    label: str


def comparison(p: Period, now: datetime | None = None) -> Comparison:
    """Same length, shifted back. If the period is still running, only up to the same time of
    day: "today until 14:10" is compared with "last Saturday until 14:10" (a fair comparison)."""
    now = now or utcnow()
    shift = 7 if p.days <= 7 else p.days
    start = local_start(p.first - timedelta(days=shift), p.tz)
    if p.end > now:
        local_now = now.astimezone(p.zone).replace(tzinfo=None)
        end = (local_now - timedelta(days=shift)).replace(tzinfo=p.zone)
    else:
        end = local_start(p.last + timedelta(days=1 - shift), p.tz)
    if shift == 7:
        label = "same time last week" if p.end > now else "last week"
    else:
        label = f"previous {shift} days"
    return Comparison(
        shift, start, end, p.first - timedelta(days=shift), p.last - timedelta(days=shift), label
    )


# --------------------------------------------------------------------------- cameras and lines


@dataclass
class SiteCameras:
    cameras: list[Camera]
    names: dict[uuid.UUID, str]
    zone_kinds: dict[tuple[uuid.UUID, str], str]
    schedules: dict[uuid.UUID, Any]  # ScheduleDoc | None
    current_lines: list[tuple[uuid.UUID, str]]

    @property
    def ids(self) -> list[uuid.UUID]:
        return [c.id for c in self.cameras]


def site_cameras(db: Session, site: Site) -> SiteCameras:
    """All cameras of the site (also of removed devices: their history still counts)."""
    cams = list(
        db.scalars(
            select(Camera)
            .join(Device, Device.id == Camera.device_id)
            .where(Device.site_id == site.id)
            .order_by(Camera.name, Camera.edge_camera_id)
        )
    )
    names = {c.id: c.name for c in cams}
    kinds: dict[tuple[uuid.UUID, str], str] = {}
    schedules: dict[uuid.UUID, Any] = {}
    lines: list[tuple[uuid.UUID, str]] = []
    for c in cams:
        doc, _ = camera_config.effective_config(c)
        schedules[c.id] = doc.schedule if doc else None
        if doc:
            for z in doc.zones:
                kinds[(c.id, z.name)] = z.kind
            lines.extend((c.id, ln.name) for ln in doc.lines)
        else:
            lines.extend((c.id, name) for name in (c.lines or []))
    return SiteCameras(cams, names, kinds, schedules, lines)


def visitor_filter(site: Site) -> set[tuple[str, str]] | None:
    """{(camera_id, line)} counted as visitors, or None = every line."""
    if site.visitor_lines is None:
        return None
    return {(str(v["camera_id"]), str(v["line"])) for v in site.visitor_lines}


def is_visitor(selection: set[tuple[str, str]] | None, cam: uuid.UUID, line: str) -> bool:
    return selection is None or (str(cam), line) in selection


# --------------------------------------------------------------------------- raw hourly data


def _local(col, tz: str, unit: str = "hour"):
    """date_trunc in the site's local time -> a timestamp without time zone."""
    return func.date_trunc(unit, func.timezone(tz, col))


def _cutoffs(ids: list[uuid.UUID]):
    """Per camera: end of the newest analysed minute. Older minutes come from the minute rows,
    newer crossings from the single events. A camera without any coverage row yet (it started
    less than a minute ago) is counted from its events only."""
    return (
        select(
            Coverage.camera_id.label("camera_id"),
            (func.max(Coverage.window_start) + literal(timedelta(seconds=60))).label("cut"),
        )
        .where(Coverage.camera_id.in_(ids))
        .group_by(Coverage.camera_id)
        .subquery()
    )


LineKey = tuple[uuid.UUID, str, str, datetime]  # camera, line, class, local hour (naive)


def line_hours(
    db: Session, ids: list[uuid.UUID], start: datetime, end: datetime, tz: str, unit: str = "hour"
) -> dict[LineKey, list[int]]:
    """IN/OUT per camera, line, class (incl. '*') and local hour (or minute/day with ``unit``)."""
    out: dict[LineKey, list[int]] = defaultdict(lambda: [0, 0])
    if not ids or end <= start:
        return out
    cut = _cutoffs(ids)
    bucket = _local(LineCount.window_start, tz, unit)
    minute_rows = db.execute(
        select(
            LineCount.camera_id,
            LineCount.line,
            LineCount.class_name,
            bucket,
            func.sum(LineCount.in_count),
            func.sum(LineCount.out_count),
        )
        .join(cut, cut.c.camera_id == LineCount.camera_id)
        .where(
            LineCount.camera_id.in_(ids),
            LineCount.window_start >= start,
            LineCount.window_start < end,
            LineCount.window_start < cut.c.cut,
        )
        .group_by(LineCount.camera_id, LineCount.line, LineCount.class_name, bucket)
    )
    for cam, line, cls, hour, n_in, n_out in minute_rows:
        acc = out[(cam, line, cls, hour)]
        acc[0] += int(n_in or 0)
        acc[1] += int(n_out or 0)

    ev_bucket = _local(CountEvent.ts, tz, unit)
    tail = db.execute(
        select(
            CountEvent.camera_id,
            CountEvent.name,
            func.coalesce(CountEvent.class_name, "unknown"),
            ev_bucket,
            func.sum(case((CountEvent.direction == "in", 1), else_=0)),
            func.sum(case((CountEvent.direction == "out", 1), else_=0)),
        )
        .outerjoin(cut, cut.c.camera_id == CountEvent.camera_id)
        .where(
            CountEvent.camera_id.in_(ids),
            CountEvent.kind == "line_cross",
            CountEvent.direction.in_(("in", "out")),
            CountEvent.ts >= start,
            CountEvent.ts < end,
            or_(cut.c.cut.is_(None), CountEvent.ts >= cut.c.cut),
        )
        .group_by(CountEvent.camera_id, CountEvent.name, CountEvent.class_name, ev_bucket)
    )
    for cam, line, cls, hour, n_in, n_out in tail:
        for c in (cls, ALL):
            acc = out[(cam, line, c, hour)]
            acc[0] += int(n_in or 0)
            acc[1] += int(n_out or 0)
    return out


def coverage_hours(
    db: Session, ids: list[uuid.UUID], start: datetime, end: datetime, tz: str, unit: str = "hour"
) -> dict[tuple[uuid.UUID, datetime], float]:
    """Analysed seconds per camera and local hour."""
    if not ids or end <= start:
        return {}
    bucket = _local(Coverage.window_start, tz, unit)
    rows = db.execute(
        select(Coverage.camera_id, bucket, func.sum(Coverage.seconds))
        .where(Coverage.camera_id.in_(ids), Coverage.window_start >= start, Coverage.window_start < end)
        .group_by(Coverage.camera_id, bucket)
    )
    return {(cam, hour): float(s or 0) for cam, hour, s in rows}


@dataclass
class ZoneAcc:
    occ_weighted: float = 0.0
    seconds: float = 0.0
    occ_max: int = 0
    queue_weighted: float = 0.0
    queue_max: int = 0
    visits: int = 0
    dwell_sum: float = 0.0

    def add(self, other: ZoneAcc) -> None:
        self.occ_weighted += other.occ_weighted
        self.seconds += other.seconds
        self.occ_max = max(self.occ_max, other.occ_max)
        self.queue_weighted += other.queue_weighted
        self.queue_max = max(self.queue_max, other.queue_max)
        self.visits += other.visits
        self.dwell_sum += other.dwell_sum

    @property
    def occ_avg(self) -> float | None:
        return self.occ_weighted / self.seconds if self.seconds > 0 else None

    @property
    def queue_avg(self) -> float | None:
        return self.queue_weighted / self.seconds if self.seconds > 0 else None

    @property
    def dwell_avg(self) -> float | None:
        return self.dwell_sum / self.visits if self.visits else None


def zone_hours(
    db: Session, ids: list[uuid.UUID], start: datetime, end: datetime, tz: str, unit: str = "hour"
) -> dict[tuple[uuid.UUID, str, datetime], ZoneAcc]:
    """Zone numbers (all classes together) per camera, zone and local hour."""
    if not ids or end <= start:
        return {}
    bucket = _local(ZoneStat.window_start, tz, unit)
    rows = db.execute(
        select(
            ZoneStat.camera_id,
            ZoneStat.zone,
            bucket,
            func.sum(ZoneStat.occ_avg * ZoneStat.sample_s),
            func.sum(ZoneStat.sample_s),
            func.max(ZoneStat.occ_max),
            func.sum(ZoneStat.queue_avg * ZoneStat.sample_s),
            func.max(ZoneStat.queue_max),
            func.sum(ZoneStat.visits),
            func.sum(ZoneStat.dwell_sum_s),
        )
        .where(
            ZoneStat.camera_id.in_(ids),
            ZoneStat.class_name == ALL,
            ZoneStat.window_start >= start,
            ZoneStat.window_start < end,
        )
        .group_by(ZoneStat.camera_id, ZoneStat.zone, bucket)
    )
    return {
        (cam, zone, hour): ZoneAcc(
            float(ow or 0),
            float(s or 0),
            int(om or 0),
            float(qw or 0),
            int(qm or 0),
            int(v or 0),
            float(ds or 0),
        )
        for cam, zone, hour, ow, s, om, qw, qm, v, ds in rows
    }


def dwell_distribution(db: Session, ids: list[uuid.UUID], start: datetime, end: datetime) -> dict[str, Any]:
    """How long visits in zones lasted: histogram, average and median (from zone_visit events)."""
    empty = {
        "count": 0,
        "avg_s": None,
        "median_s": None,
        "buckets": [{"label": lbl, "count": 0} for lbl in DWELL_LABELS],
    }
    if not ids or end <= start:
        return empty
    where = and_(
        CountEvent.camera_id.in_(ids),
        CountEvent.kind == "zone_visit",
        CountEvent.dwell_s.is_not(None),
        CountEvent.ts >= start,
        CountEvent.ts < end,
    )
    n, avg, median = db.execute(
        select(
            func.count(),
            func.avg(CountEvent.dwell_s),
            func.percentile_cont(0.5).within_group(CountEvent.dwell_s),
        ).where(where)
    ).one()
    if not n:
        return empty
    bucket = case(
        *[(CountEvent.dwell_s < edge, i) for i, edge in enumerate(DWELL_EDGES)], else_=len(DWELL_EDGES)
    )
    counts = dict(db.execute(select(bucket, func.count()).where(where).group_by(bucket)).all())
    return {
        "count": int(n),
        "avg_s": round(float(avg), 1),
        "median_s": round(float(median), 1),
        "buckets": [{"label": lbl, "count": int(counts.get(i, 0))} for i, lbl in enumerate(DWELL_LABELS)],
    }


def occupancy_now(db: Session, sc: SiteCameras, now: datetime | None = None) -> list[dict[str, Any]]:
    """Newest occupancy per zone, if it is at most 5 minutes old."""
    now = now or utcnow()
    if not sc.ids:
        return []
    rows = db.execute(
        select(ZoneStat.camera_id, ZoneStat.zone, ZoneStat.occ_last, ZoneStat.window_start)
        .where(
            ZoneStat.camera_id.in_(sc.ids),
            ZoneStat.class_name == ALL,
            ZoneStat.window_start >= now - timedelta(seconds=OCCUPANCY_FRESH_S),
        )
        .ext(distinct_on(ZoneStat.camera_id, ZoneStat.zone))
        .order_by(ZoneStat.camera_id, ZoneStat.zone, ZoneStat.window_start.desc())
    )
    return [
        {
            "camera_id": cam,
            "camera_name": sc.names.get(cam, "?"),
            "zone": zone,
            "kind": sc.zone_kinds.get((cam, zone), "area"),
            "value": int(occ),
            "as_of": (ws + timedelta(seconds=60)),
        }
        for cam, zone, occ, ws in rows
    ]


# --------------------------------------------------------------------------- coverage


def expected_seconds(p_start: datetime, p_end: datetime, tz: str, schedule) -> float:
    """Seconds a camera should have counted between two times (only inside its counting hours)."""
    if p_end <= p_start:
        return 0.0
    if schedule is None:
        return (p_end - p_start).total_seconds()
    zone = ZoneInfo(tz)
    h1, m1 = map(int, schedule.start.split(":"))
    h2, m2 = map(int, schedule.end.split(":"))
    total = 0.0
    day = p_start.astimezone(zone).date() - timedelta(days=1)  # a window over midnight starts the day before
    last = p_end.astimezone(zone).date()
    while day <= last:
        if day.weekday() in schedule.days:
            w_start = datetime.combine(day, time(h1, m1), tzinfo=zone)
            end_day = day if (h2, m2) > (h1, m1) else day + timedelta(days=1)
            w_end = datetime.combine(end_day, time(h2, m2), tzinfo=zone)
            lo, hi = max(w_start, p_start), min(w_end, p_end)
            if hi > lo:
                total += (hi - lo).total_seconds()
        day += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- the dashboard


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def _change(now_v: float, before: float, comparable: bool = True) -> float | None:
    """Change in percent; None when there is nothing (fair) to compare with."""
    if before <= 0 or not comparable:
        return None
    return round((now_v - before) / before * 100, 1)


def comparable(
    db: Session, ids: list[uuid.UUID], cur: tuple[datetime, datetime], old: tuple[datetime, datetime]
) -> bool:
    """False when the cameras ran much less in the comparison period (for example the first
    weeks after the installation): "+467 %" would only measure the missing data, not the visitors.
    Without coverage rows at all (very old agents) we cannot tell, so we compare."""
    if not ids:
        return True

    def seconds(a: datetime, b: datetime) -> float:
        if b <= a:
            return 0.0
        return float(
            db.scalar(
                select(func.coalesce(func.sum(Coverage.seconds), 0)).where(
                    Coverage.camera_id.in_(ids), Coverage.window_start >= a, Coverage.window_start < b
                )
            )
            or 0
        )

    now_s, before_s = seconds(*cur), seconds(*old)
    return now_s <= 0 or before_s >= 0.5 * now_s


@dataclass
class _Lines:
    """Line numbers summed for the dashboard."""

    per_line: dict[tuple[uuid.UUID, str], list[int]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0])
    )
    visitor_hours: dict[datetime, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    classes: dict[str, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))


def _sum_lines(raw: dict[LineKey, list[int]], selection) -> _Lines:
    acc = _Lines()
    for (cam, line, cls, hour), (n_in, n_out) in raw.items():
        visitor = is_visitor(selection, cam, line)
        if cls == ALL:
            pl = acc.per_line[(cam, line)]
            pl[0] += n_in
            pl[1] += n_out
            if visitor:
                vh = acc.visitor_hours[hour]
                vh[0] += n_in
                vh[1] += n_out
        elif visitor:
            c = acc.classes[cls]
            c[0] += n_in
            c[1] += n_out
    return acc


def _buckets(p: Period) -> list[datetime]:
    """All local bucket starts of the period (naive local times)."""
    start = datetime.combine(p.first, time.min)
    if p.bucket == "hour":
        return [start + timedelta(hours=h) for h in range(24 * p.days)]
    return [start + timedelta(days=d) for d in range(p.days)]


def _to_bucket(hour: datetime, unit: str) -> datetime:
    return hour if unit == "hour" else datetime.combine(hour.date(), time.min)


def dashboard(db: Session, site: Site, p: Period, now: datetime | None = None) -> dict[str, Any]:
    """Everything the site dashboard shows, for one period."""
    now = now or utcnow()
    sc = site_cameras(db, site)
    cmp = comparison(p, now)
    selection = visitor_filter(site)
    tz = site.timezone
    live = p.start <= now < p.end
    until = min(p.end, now)
    local_now = now.astimezone(p.zone).replace(tzinfo=None)

    cur = _sum_lines(line_hours(db, sc.ids, p.start, until, tz), selection)
    old = _sum_lines(line_hours(db, sc.ids, cmp.start, cmp.end, tz), selection)
    fair = comparable(db, sc.ids, (p.start, until), (cmp.start, cmp.end))

    # -- lines --------------------------------------------------------------------------------
    keys = list(dict.fromkeys([*sc.current_lines, *cur.per_line.keys(), *old.per_line.keys()]))
    lines = []
    for cam, line in keys:
        n_in, n_out = cur.per_line.get((cam, line), [0, 0])
        o_in, o_out = old.per_line.get((cam, line), [0, 0])
        lines.append(
            {
                "camera_id": cam,
                "camera_name": sc.names.get(cam, "?"),
                "line": line,
                "visitor": is_visitor(selection, cam, line),
                "in": n_in,
                "out": n_out,
                "compare_in": o_in,
                "compare_out": o_out,
                "change_pct": _change(n_in, o_in, fair),
            }
        )
    lines.sort(key=lambda r: (not r["visitor"], r["camera_name"].lower(), r["line"].lower()))

    total_in = sum(v[0] for v in cur.visitor_hours.values())
    total_out = sum(v[1] for v in cur.visitor_hours.values())
    cmp_in = sum(v[0] for v in old.visitor_hours.values())
    cmp_out = sum(v[1] for v in old.visitor_hours.values())

    # -- time series ---------------------------------------------------------------------------
    unit = p.bucket
    by_bucket: dict[datetime, list[int]] = defaultdict(lambda: [0, 0])
    for hour, (n_in, n_out) in cur.visitor_hours.items():
        b = by_bucket[_to_bucket(hour, unit)]
        b[0] += n_in
        b[1] += n_out
    cmp_bucket: dict[datetime, int] = defaultdict(int)
    for hour, (n_in, _) in old.visitor_hours.items():
        cmp_bucket[_to_bucket(hour + timedelta(days=cmp.shift_days), unit)] += n_in
    series = []
    for b in _buckets(p):
        future = b > local_now
        n_in, n_out = by_bucket.get(b, [0, 0])
        series.append(
            {
                "t": _iso(b),
                "in": None if future else n_in,
                "out": None if future else n_out,
                "compare_in": cmp_bucket.get(b, 0),
            }
        )

    peak = None
    if cur.visitor_hours:
        hour, (n_in, _) = max(cur.visitor_hours.items(), key=lambda kv: (kv[1][0], -kv[0].timestamp()))
        if n_in > 0:
            peak = {"t": _iso(hour), "in": n_in}

    # -- zones ---------------------------------------------------------------------------------
    zraw = zone_hours(db, sc.ids, p.start, until, tz)
    zones_acc: dict[tuple[uuid.UUID, str], ZoneAcc] = defaultdict(ZoneAcc)
    zseries: dict[tuple[uuid.UUID, str], dict[datetime, ZoneAcc]] = defaultdict(lambda: defaultdict(ZoneAcc))
    for (cam, zone, hour), acc in zraw.items():
        zones_acc[(cam, zone)].add(acc)
        zseries[(cam, zone)][_to_bucket(hour, unit)].add(acc)
    zone_keys = list(dict.fromkeys([*sc.zone_kinds.keys(), *zones_acc.keys()]))
    zones = []
    for cam, zone in zone_keys:
        acc = zones_acc.get((cam, zone), ZoneAcc())
        kind = sc.zone_kinds.get((cam, zone), "area")
        series_z = []
        for b in _buckets(p):
            za = zseries.get((cam, zone), {}).get(b)
            avg = za.queue_avg if (za and kind == "queue") else (za.occ_avg if za else None)
            mx = (za.queue_max if kind == "queue" else za.occ_max) if za and za.seconds > 0 else None
            series_z.append({"t": _iso(b), "avg": None if avg is None else round(avg, 2), "max": mx})
        zones.append(
            {
                "camera_id": cam,
                "camera_name": sc.names.get(cam, "?"),
                "zone": zone,
                "kind": kind,
                "occ_avg": None if acc.occ_avg is None else round(acc.occ_avg, 2),
                "occ_max": acc.occ_max,
                "queue_avg": None if acc.queue_avg is None else round(acc.queue_avg, 2),
                "queue_max": acc.queue_max,
                "visits": acc.visits,
                "dwell_avg_s": None if acc.dwell_avg is None else round(acc.dwell_avg, 1),
                "series": series_z,
            }
        )

    # -- coverage ------------------------------------------------------------------------------
    cov = coverage_hours(db, sc.ids, p.start, until, tz)
    first_cov = (
        dict(
            db.execute(
                select(Coverage.camera_id, func.min(Coverage.window_start))
                .where(Coverage.camera_id.in_(sc.ids))
                .group_by(Coverage.camera_id)
            ).all()
        )
        if sc.ids
        else {}
    )
    cov_cam: dict[uuid.UUID, float] = defaultdict(float)
    for (cam, _), s in cov.items():
        cov_cam[cam] += s
    cams_cov = []
    for c in sc.cameras:
        # a camera added during the period is only expected from then on (its first data can be
        # older than the camera row: the edge uploads its backlog after pairing)
        since = min(c.created_at, first_cov.get(c.id, c.created_at))
        expected = expected_seconds(max(p.start, since), until, tz, sc.schedules.get(c.id))
        got = cov_cam.get(c.id, 0.0)
        if expected <= 0 and got <= 0:
            continue
        cams_cov.append(
            {
                "camera_id": c.id,
                "camera_name": c.name,
                "seconds": round(got),
                "expected_s": round(expected),
                "pct": None if expected <= 0 else round(min(100.0, got / expected * 100), 1),
            }
        )
    exp_total = sum(c["expected_s"] for c in cams_cov)
    coverage_pct = (
        round(min(100.0, sum(c["seconds"] for c in cams_cov) / exp_total * 100), 1) if exp_total else None
    )

    # -- now -----------------------------------------------------------------------------------
    occ = occupancy_now(db, sc, now) if live else []
    inside = None
    if live:
        today_start = local_start(local_today(tz, now), tz)
        today = (
            _sum_lines(line_hours(db, sc.ids, today_start, now, tz), selection)
            if today_start != p.start or p.days != 1
            else cur
        )
        t_in = sum(v[0] for v in today.visitor_hours.values())
        t_out = sum(v[1] for v in today.visitor_hours.values())
        inside = max(0, t_in - t_out)

    return {
        "site": {"id": site.id, "name": site.name, "timezone": tz},
        "period": {
            "from": p.first.isoformat(),
            "to": p.last.isoformat(),
            "days": p.days,
            "bucket": unit,
            "start": p.start,
            "end": p.end,
            "live": live,
        },
        "compare": {
            "from": cmp.first.isoformat(),
            "to": cmp.last.isoformat(),
            "start": cmp.start,
            "end": cmp.end,
            "label": cmp.label,
            "shift_days": cmp.shift_days,
        },
        "visitor_lines": site.visitor_lines,
        "totals": {
            "in": total_in,
            "out": total_out,
            "compare_in": cmp_in,
            "compare_out": cmp_out,
            "change_pct": _change(total_in, cmp_in, fair),
            "comparable": fair,
            "inside_estimate": inside,
        },
        "series": series,
        "peak": peak,
        "lines": lines,
        "classes": sorted(
            ({"class_name": k, "in": v[0], "out": v[1]} for k, v in cur.classes.items()),
            key=lambda r: -r["in"],
        ),
        "zones": zones,
        "occupancy_now": occ,
        "dwell": dwell_distribution(db, sc.ids, p.start, until),
        "heatmap": heatmap(db, site, sc, selection, p, now),
        "coverage": {"pct": coverage_pct, "cameras": cams_cov},
        "camera_count": len(sc.cameras),
        "has_data": bool(cur.per_line or zraw or cov),
        "generated_at": now,
    }


def heatmap(
    db: Session, site: Site, sc: SiteCameras, selection, p: Period, now: datetime | None = None
) -> dict[str, Any]:
    """Average visitors per weekday and hour.

    Uses the period if it has at least 4 weeks, else the 4 weeks that end with the period.
    The average only counts days on which the cameras were really running in that hour, so a
    day with a broken camera does not pull the numbers down.
    """
    now = now or utcnow()
    tz = site.timezone
    last = min(p.last, local_today(tz, now))
    first = p.first if p.days >= HEATMAP_DAYS else last - timedelta(days=HEATMAP_DAYS - 1)
    start, end = local_start(first, tz), min(local_start(last + timedelta(days=1), tz), now)
    raw = _sum_lines(line_hours(db, sc.ids, start, end, tz), selection)
    visitor_cams = {
        cam for cam, line in [*sc.current_lines, *raw.per_line.keys()] if is_visitor(selection, cam, line)
    }
    cov = coverage_hours(db, list(visitor_cams), start, end, tz)
    covered = {hour for (_, hour), s in cov.items() if s > 0}
    sums: dict[tuple[int, int], int] = defaultdict(int)
    days: dict[tuple[int, int], int] = defaultdict(int)
    for hour in covered:
        days[(hour.weekday(), hour.hour)] += 1
    for hour, (n_in, _) in raw.visitor_hours.items():
        sums[(hour.weekday(), hour.hour)] += n_in
        if hour not in covered:  # counts without coverage rows (old data): still a day with data
            days[(hour.weekday(), hour.hour)] += 1
    cells = []
    for wd in range(7):
        for h in range(24):
            d = days.get((wd, h), 0)
            cells.append(
                {
                    "weekday": wd,
                    "hour": h,
                    "days": d,
                    "avg_in": round(sums.get((wd, h), 0) / d, 1) if d else None,
                }
            )
    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "cells": cells,
        "max": max((c["avg_in"] or 0 for c in cells), default=0),
    }


def site_today(db: Session, site: Site, now: datetime | None = None) -> dict[str, Any]:
    """Small summary for the overview page: visitors today vs. the same time last week."""
    now = now or utcnow()
    p = parse_period(site.timezone, None, None, now)
    sc = site_cameras(db, site)
    cmp = comparison(p, now)
    selection = visitor_filter(site)
    cur = _sum_lines(line_hours(db, sc.ids, p.start, now, site.timezone), selection)
    old = _sum_lines(line_hours(db, sc.ids, cmp.start, cmp.end, site.timezone), selection)
    t_in = sum(v[0] for v in cur.visitor_hours.values())
    t_out = sum(v[1] for v in cur.visitor_hours.values())
    c_in = sum(v[0] for v in old.visitor_hours.values())
    occ = occupancy_now(db, sc, now)
    areas = [o for o in occ if o["kind"] == "area"]
    queues = [o for o in occ if o["kind"] == "queue"]
    hours = {h.hour: v[0] for h, v in cur.visitor_hours.items()}
    local_hour = now.astimezone(p.zone).hour
    return {
        "site_id": site.id,
        "name": site.name,
        "timezone": site.timezone,
        "camera_count": len(sc.cameras),
        "today_in": t_in,
        "today_out": t_out,
        "compare_in": c_in,
        "change_pct": _change(t_in, c_in, comparable(db, sc.ids, (p.start, now), (cmp.start, cmp.end))),
        "compare_label": cmp.label,
        "inside_estimate": max(0, t_in - t_out),
        "occupancy": sum(o["value"] for o in areas) if areas else None,
        "queue": sum(o["value"] for o in queues) if queues else None,
        "hours": [hours.get(h, 0) if h <= local_hour else None for h in range(24)],
    }
