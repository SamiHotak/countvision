"""Sample data: a "Demo café" with 5 weeks of realistic numbers, to try dashboards and reports.

Real data takes weeks to collect, so this fills one site with invented numbers: opening hours
08:00-19:00, busier at lunch and on Saturdays, closed on Sundays, a counter queue zone and one
camera outage (so the coverage warning can be seen). Everything is clearly named as sample data
and lives in its own site with a removed (revoked) device, so it never mixes with real cameras.
Delete the site's device and then the site to remove it again.

Same random seed -> the same numbers every time (useful for screenshots and tests).
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.models import Organization
from ..models import Camera, CountEvent, Coverage, Device, LineCount, Site, ZoneStat

log = logging.getLogger(__name__)

SITE_NAME = "Demo café (sample data)"
OPEN, CLOSE = 8, 19


def _hour_weight(hour: int, weekday: int) -> float:
    """Visitors per hour (average) for an invented café."""
    if weekday == 6 or not OPEN <= hour < CLOSE:
        return 0.0
    base = 6 + 14 * math.exp(-((hour - 12.5) ** 2) / 2.2) + 6 * math.exp(-((hour - 16.5) ** 2) / 1.5)
    if hour == 8:
        base += 6  # morning coffee
    return base * (1.45 if weekday == 5 else 1.0 if weekday < 4 else 1.15)


def _insert(db: Session, model, rows: list[dict]) -> None:
    """Fast bulk insert (SQLAlchemy batches the rows into multi-row INSERTs by itself)."""
    keys = sorted({k for r in rows for k in r})  # every row needs the same columns
    if rows:
        db.execute(insert(model), [{k: r.get(k) for k in keys} for r in rows])


def create(
    db: Session, org: Organization, days: int = 35, seed: int = 7, now: datetime | None = None
) -> Site:
    """Create the sample site with ``days`` days of data up to now. Returns the site."""
    now = now or utcnow()
    tz = "Europe/Berlin"
    zone = ZoneInfo(tz)
    rnd = random.Random(seed)
    site = Site(org_id=org.id, name=SITE_NAME, timezone=tz, address="Sample data, not a real place")
    db.add(site)
    db.flush()
    token = hashlib.sha256(uuid.uuid4().bytes).hexdigest()  # nobody knows the token: no real device
    device = Device(
        org_id=org.id,
        site_id=site.id,
        name="Sample device",
        edge_device_id="sample",
        token_hash=token,
        agent_version="sample",
        revoked_at=now,
        last_seen_at=now,
    )
    db.add(device)
    db.flush()
    config = {
        "lines": [{"name": "Door", "p1": [0.55, 0.15], "p2": [0.55, 0.95], "in_direction": "to_right"}],
        "zones": [
            {
                "name": "Counter queue",
                "polygon": [[0.1, 0.2], [0.4, 0.2], [0.4, 0.7], [0.1, 0.7]],
                "kind": "queue",
            }
        ],
        "classes": ["person", "bicycle"],
        "anchor": "bottom_center",
        "schedule": {"days": [0, 1, 2, 3, 4, 5], "start": "07:30", "end": "19:30"},
    }
    cam = Camera(
        org_id=org.id,
        device_id=device.id,
        edge_camera_id="entrance",
        name="Entrance",
        state="stopped",
        lines=["Door"],
        zones=["Counter queue"],
        reported_config=config,
        last_seen_at=now,
        created_at=now - timedelta(days=days + 1),  # "installed" before the first sample day
    )
    db.add(cam)
    db.flush()

    lines, cover, zones, events = [], [], [], []
    today = now.astimezone(zone).date()
    outage_day = today - timedelta(days=9)  # one afternoon without data
    n_ev = 0
    for back in range(days, -1, -1):
        day: date = today - timedelta(days=back)
        if day.weekday() == 6:
            continue
        exits: dict[int, int] = {}  # minute of the day -> people leaving (they stay 8-45 min)
        for hour in range(7, 20):
            for minute in range(30, 60) if hour == 7 else range(0, 30) if hour == 19 else range(60):
                start = datetime.combine(day, time(hour, minute), tzinfo=zone)
                if start + timedelta(seconds=60) > now:
                    break
                if day == outage_day and 14 <= hour < 17:
                    continue
                ws = start
                cover.append({"camera_id": cam.id, "window_start": ws, "frames": 600, "seconds": 60.0})
                rate = _hour_weight(hour, day.weekday()) / 60
                n_in = sum(1 for _ in range(4) if rnd.random() < rate / 4) if rate else 0
                n_in += 1 if rate > 0.3 and rnd.random() < rate - 0.3 else 0
                mod = hour * 60 + minute
                for _ in range(n_in):
                    leave = min(mod + rnd.randint(8, 45), 19 * 60 + 29)
                    exits[leave] = exits.get(leave, 0) + 1
                n_out = exits.pop(mod, 0)
                bikes = 1 if n_in and rnd.random() < 0.06 else 0
                if n_in or n_out:
                    lines.append(
                        {
                            "camera_id": cam.id,
                            "window_start": ws,
                            "line": "Door",
                            "class_name": "*",
                            "in_count": n_in,
                            "out_count": n_out,
                        }
                    )
                    lines.append(
                        {
                            "camera_id": cam.id,
                            "window_start": ws,
                            "line": "Door",
                            "class_name": "person",
                            "in_count": n_in - bikes,
                            "out_count": n_out,
                        }
                    )
                    if bikes:
                        lines.append(
                            {
                                "camera_id": cam.id,
                                "window_start": ws,
                                "line": "Door",
                                "class_name": "bicycle",
                                "in_count": bikes,
                                "out_count": 0,
                            }
                        )
                    for direction, n in (("in", n_in), ("out", n_out)):
                        for k in range(n):
                            n_ev += 1
                            cls = "bicycle" if direction == "in" and k < bikes else "person"
                            events.append(
                                {
                                    "camera_id": cam.id,
                                    "ts": ws + timedelta(seconds=rnd.uniform(0, 59)),
                                    "event_id": f"s{n_ev}",
                                    "kind": "line_cross",
                                    "name": "Door",
                                    "class_name": cls,
                                    "direction": direction,
                                }
                            )
                # queue at the counter: grows with traffic
                q = max(0.0, rate * 2.2 + rnd.uniform(-0.5, 0.5)) if rate else 0.0
                visits = n_in - bikes if rate else 0
                dwell = [max(15.0, rnd.gauss(70 + 60 * q, 35)) for _ in range(visits)]
                zones.append(
                    {
                        "camera_id": cam.id,
                        "window_start": ws,
                        "zone": "Counter queue",
                        "class_name": "*",
                        "sample_s": 60.0,
                        "occ_avg": round(q, 2),
                        "occ_max": math.ceil(q + rnd.random()),
                        "occ_last": round(q),
                        "queue_avg": round(q, 2),
                        "queue_max": math.ceil(q + rnd.random()),
                        "visits": visits,
                        "dwell_sum_s": round(sum(dwell), 1),
                        "dwell_max_s": round(max(dwell, default=0), 1),
                    }
                )
                for d in dwell:
                    n_ev += 1
                    events.append(
                        {
                            "camera_id": cam.id,
                            "ts": ws + timedelta(seconds=rnd.uniform(0, 59)),
                            "event_id": f"s{n_ev}",
                            "kind": "zone_visit",
                            "name": "Counter queue",
                            "class_name": "person",
                            "dwell_s": round(d, 1),
                        }
                    )
    _insert(db, Coverage, cover)
    _insert(db, LineCount, lines)
    _insert(db, ZoneStat, zones)
    _insert(db, CountEvent, events)
    device.last_data_at = now
    # Fresh table statistics: without them PostgreSQL plans the first dashboard query badly
    # after so many new rows at once (seconds instead of milliseconds).
    db.flush()
    for table in ("coverage", "line_counts", "zone_stats", "count_events"):
        db.execute(text(f"ANALYZE {table}"))
    log.info(
        "Sample site %s: %d minutes, %d line rows, %d events", site.id, len(cover), len(lines), len(events)
    )
    return site


def remove(db: Session, org: Organization) -> bool:
    """Delete the sample site with its device(s); cameras and numbers go with them (cascade)."""
    site = db.scalar(select(Site).where(Site.org_id == org.id, Site.name == SITE_NAME))
    if site is None:
        return False
    db.execute(delete(Device).where(Device.site_id == site.id))  # the database cascades cameras + data
    db.delete(site)
    db.flush()
    return True
