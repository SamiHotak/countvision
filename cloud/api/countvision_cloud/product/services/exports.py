"""CSV exports of a site's numbers (lines or zones), in the site's local time.

Excel in Germany opens ";"-separated files with decimal commas; Excel in English with "," and
dots. So the separator is a parameter and decimals follow it. The file starts with a UTF-8 BOM,
so Excel shows umlauts correctly.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError
from ..models import Site
from . import analytics

Bucket = Literal["minute", "hour", "day"]
MAX_MINUTE_DAYS = 7


def _num(value: float | None, decimal_comma: bool, digits: int = 2) -> str:
    if value is None:
        return ""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".") if digits else str(int(value))
    return text.replace(".", ",") if decimal_comma else text


def _fmt(bucket_start: datetime, bucket: Bucket) -> tuple[str, str]:
    return bucket_start.strftime("%Y-%m-%d"), "" if bucket == "day" else bucket_start.strftime("%H:%M")


def lines_csv(
    db: Session,
    site: Site,
    p: analytics.Period,
    bucket: Bucket = "hour",
    delimiter: str = ",",
    now: datetime | None = None,
) -> str:
    """One row per time bucket, camera and line: IN/OUT in total and per class."""
    if bucket == "minute" and p.days > MAX_MINUTE_DAYS:
        raise AppError(
            f"Minute rows are available for up to {MAX_MINUTE_DAYS} days. Choose hours or days.",
            code="bad_period",
            status=422,
        )
    now = now or utcnow()
    sc = analytics.site_cameras(db, site)
    raw = analytics.line_hours(db, sc.ids, p.start, min(p.end, now), site.timezone, unit=bucket)
    selection = analytics.visitor_filter(site)
    classes = sorted({cls for (_, _, cls, _) in raw if cls != analytics.ALL})
    rows: dict[tuple[datetime, str, str], dict[str, list[int]]] = defaultdict(dict)
    visitor: dict[tuple[str, str], bool] = {}
    for (cam, line, cls, b), counts in raw.items():
        name = sc.names.get(cam, "?")
        rows[(b, name, line)][cls] = counts
        visitor[(name, line)] = analytics.is_visitor(selection, cam, line)

    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\r\n")
    head = ["date", "time" if bucket != "day" else "", "camera", "line", "counts_as_visitors", "in", "out"]
    head = [h for h in head if h]
    for cls in classes:
        head += [f"in_{cls}", f"out_{cls}"]
    writer.writerow(head)
    for b, cam_name, line in sorted(rows):
        per = rows[(b, cam_name, line)]
        total = per.get(analytics.ALL, [0, 0])
        d, t = _fmt(b, bucket)
        row = (
            [d]
            + ([t] if bucket != "day" else [])
            + [cam_name, line, "yes" if visitor[(cam_name, line)] else "no", total[0], total[1]]
        )
        for cls in classes:
            c = per.get(cls, [0, 0])
            row += [c[0], c[1]]
        writer.writerow(row)
    return out.getvalue()


def zones_csv(
    db: Session,
    site: Site,
    p: analytics.Period,
    bucket: Bucket = "hour",
    delimiter: str = ",",
    now: datetime | None = None,
) -> str:
    """One row per time bucket, camera and zone: occupancy, queue, visits, dwell."""
    if bucket == "minute" and p.days > MAX_MINUTE_DAYS:
        raise AppError(
            f"Minute rows are available for up to {MAX_MINUTE_DAYS} days. Choose hours or days.",
            code="bad_period",
            status=422,
        )
    now = now or utcnow()
    sc = analytics.site_cameras(db, site)
    raw = analytics.zone_hours(db, sc.ids, p.start, min(p.end, now), site.timezone, unit=bucket)
    comma = delimiter == ";"
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out, delimiter=delimiter, lineterminator="\r\n")
    head = (
        ["date"]
        + (["time"] if bucket != "day" else [])
        + [
            "camera",
            "zone",
            "kind",
            "occupancy_avg",
            "occupancy_max",
            "queue_avg",
            "queue_max",
            "visits",
            "dwell_avg_s",
            "analysed_s",
        ]
    )
    writer.writerow(head)
    for (cam, zone, b), acc in sorted(
        raw.items(), key=lambda kv: (kv[0][2], sc.names.get(kv[0][0], ""), kv[0][1])
    ):
        d, t = _fmt(b, bucket)
        writer.writerow(
            [d]
            + ([t] if bucket != "day" else [])
            + [
                sc.names.get(cam, "?"),
                zone,
                sc.zone_kinds.get((cam, zone), "area"),
                _num(acc.occ_avg, comma),
                acc.occ_max,
                _num(acc.queue_avg, comma),
                acc.queue_max,
                acc.visits,
                _num(acc.dwell_avg, comma, 1),
                _num(acc.seconds, comma, 0),
            ]
        )
    return out.getvalue()


def filename(site: Site, p: analytics.Period, kind: str, ext: str) -> str:
    """A safe file name like countvision_shop-mitte_2026-10-01_2026-10-07_lines.csv"""
    slug = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in site.name.lower()).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)[:40] or "site"
    period = p.first.isoformat() if p.days == 1 else f"{p.first.isoformat()}_{p.last.isoformat()}"
    return f"countvision_{slug}_{period}_{kind}.{ext}"
