"""Numbers for the local web app, read from the SQLite buffer.

Line crossings and zone visits are read from the ``events`` table because events are written
the moment they happen (the 1-minute tables are written up to ~65 s later). Peak occupancy
comes from ``zone_stats`` (per minute) and is combined with the live value by the caller.

Times are UTC epoch seconds. "Today" is the local day of the computer that runs the app.
"""

from __future__ import annotations

import csv
import io
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta

from ..storage import SqliteBuffer


def day_bounds(now: float | None = None) -> tuple[float, float]:
    """Start and end of the local calendar day that contains ``now`` (epoch seconds)."""
    current = datetime.fromtimestamp(now) if now is not None else datetime.now()
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def line_totals(
    buffer: SqliteBuffer, camera_id: str, start: float, end: float | None = None
) -> dict[str, dict]:
    """{line: {"in": n, "out": n, "by_class": {class: {"in", "out"}}}} for [start, end)."""
    totals: dict[str, dict] = {}
    for row in buffer.query("events", camera_id=camera_id, start=start, end=end):
        if row["kind"] != "line_cross" or row["direction"] not in ("in", "out"):
            continue
        entry = totals.setdefault(row["name"], {"in": 0, "out": 0, "by_class": {}})
        entry[row["direction"]] += 1
        cls = entry["by_class"].setdefault(row["class_name"] or "?", {"in": 0, "out": 0})
        cls[row["direction"]] += 1
    return totals


def zone_totals(
    buffer: SqliteBuffer, camera_id: str, start: float, end: float | None = None
) -> dict[str, dict]:
    """{zone: {"visits", "dwell_avg_s", "dwell_max_s", "occupancy_max"}} for [start, end)."""
    acc: dict[str, dict] = defaultdict(
        lambda: {"visits": 0, "dwell_sum": 0.0, "dwell_max_s": 0.0, "occupancy_max": 0}
    )
    for row in buffer.query("events", camera_id=camera_id, start=start, end=end):
        if row["kind"] != "zone_visit" or row["dwell_s"] is None:
            continue
        zone = acc[row["name"]]
        zone["visits"] += 1
        zone["dwell_sum"] += row["dwell_s"]
        zone["dwell_max_s"] = max(zone["dwell_max_s"], row["dwell_s"])
    window_start = math.floor(start / 60) * 60
    for row in buffer.query("zone_stats", camera_id=camera_id, start=window_start, end=end):
        if row["class_name"] == "*":
            zone = acc[row["zone"]]
            zone["occupancy_max"] = max(zone["occupancy_max"], int(row["occ_max"]))
    result: dict[str, dict] = {}
    for name, zone in acc.items():
        visits = zone["visits"]
        result[name] = {
            "visits": visits,
            "dwell_avg_s": round(zone["dwell_sum"] / visits, 1) if visits else 0.0,
            "dwell_max_s": round(zone["dwell_max_s"], 1),
            "occupancy_max": zone["occupancy_max"],
        }
    return result


def line_timeline(
    buffer: SqliteBuffer,
    camera_id: str,
    start: float,
    end: float,
    bucket_s: int,
    line: str | None = None,
) -> list[dict]:
    """IN/OUT per time bucket: [{"t": bucket start, "in": n, "out": n}, ...].

    ``line`` None = all lines together. Buckets cover [start, end) without gaps.
    """
    if bucket_s <= 0:
        raise ValueError("bucket_s must be positive")
    count = max(1, math.ceil((end - start) / bucket_s))
    buckets = [{"t": start + i * bucket_s, "in": 0, "out": 0} for i in range(count)]
    for row in buffer.query("events", camera_id=camera_id, start=start, end=end):
        if row["kind"] != "line_cross" or row["direction"] not in ("in", "out"):
            continue
        if line is not None and row["name"] != line:
            continue
        index = int((row["ts"] - start) // bucket_s)
        if 0 <= index < count:
            buckets[index][row["direction"]] += 1
    return buckets


def hourly_csv(
    buffer: SqliteBuffer, camera_id: str, start: float, end: float, delimiter: str = ","
) -> str:
    """One row per local hour and line/zone: easy to open in Excel or Google Sheets.

    Columns: date, hour, camera, type, name, in, out, visits, avg_dwell_s, peak_inside.
    ``delimiter`` ";" suits Excel in countries that write decimals with a comma (Germany).
    Numbers with decimals then also use a comma.
    """
    lines: dict[tuple[str, int, str], list[int]] = defaultdict(lambda: [0, 0])
    zones: dict[tuple[str, int, str], dict] = defaultdict(
        lambda: {"visits": 0, "dwell": 0.0, "peak": 0}
    )

    def key(ts: float, name: str) -> tuple[str, int, str]:
        local = datetime.fromtimestamp(ts)
        return local.strftime("%Y-%m-%d"), local.hour, name

    for row in buffer.query("events", camera_id=camera_id, start=start, end=end):
        if row["kind"] == "line_cross" and row["direction"] in ("in", "out"):
            lines[key(row["ts"], row["name"])][0 if row["direction"] == "in" else 1] += 1
        elif row["kind"] == "zone_visit" and row["dwell_s"] is not None:
            zone = zones[key(row["ts"], row["name"])]
            zone["visits"] += 1
            zone["dwell"] += row["dwell_s"]
    for row in buffer.query("zone_stats", camera_id=camera_id, start=start, end=end):
        if row["class_name"] == "*":
            zone = zones[key(row["window_start"], row["zone"])]
            zone["peak"] = max(zone["peak"], int(row["occ_max"]))

    out = io.StringIO()
    writer = csv.writer(out, delimiter=delimiter)
    writer.writerow(
        ["date", "hour", "camera", "type", "name", "in", "out", "visits", "avg_dwell_s", "peak_inside"]
    )
    rows = [(k, "line") for k in lines] + [(k, "zone") for k in zones]
    for (date, hour, name), kind in sorted(rows, key=lambda item: (item[0], item[1])):
        if kind == "line":
            counts = lines[(date, hour, name)]
            writer.writerow([date, f"{hour:02d}:00", camera_id, "line", name, counts[0], counts[1],
                             "", "", ""])
        else:
            zone = zones[(date, hour, name)]
            avg = round(zone["dwell"] / zone["visits"], 1) if zone["visits"] else ""
            if delimiter == ";" and avg != "":
                avg = str(avg).replace(".", ",")
            writer.writerow([date, f"{hour:02d}:00", camera_id, "zone", name, "", "",
                             zone["visits"], avg, zone["peak"]])
    return out.getvalue()


def report_text(scope_kind: str, camera_id: str, start: float, end: float, lines: dict, zones: dict,
                minute_report: dict) -> str:
    """Plain-text report for the page. Counts come from events (always up to date); averages
    of people inside come from the 1-minute table (up to about one minute behind)."""
    def local(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

    title = "Video" if scope_kind == "video" else "Today"
    cam = minute_report.get("cameras", {}).get(camera_id, {})
    out = [f"{title}: camera {camera_id}, {local(start)} to {local(min(end, max(start, time.time())))}"
           if scope_kind == "today" else f"{title}: camera {camera_id}"]
    out.append(f"  measured video time: {cam.get('measured_s', 0.0):.0f} s (written once per minute)")
    if not lines and not zones:
        out.append("  No lines or zones.")
    for name, line in sorted(lines.items()):
        out.append(f"  Line '{name}': IN {line['in']}, OUT {line['out']}")
        for cls, counts in sorted(line["by_class"].items()):
            out.append(f"      {cls}: IN {counts['in']}, OUT {counts['out']}")
    for name, zone in sorted(zones.items()):
        minute = cam.get("zones", {}).get(name, {})
        out.append(
            f"  Zone '{name}': visits {zone['visits']}, average stay {zone['dwell_avg_s']} s, "
            f"longest {zone['dwell_max_s']} s | people inside: average {minute.get('occupancy_avg', 0.0)}, "
            f"most {zone['occupancy_max']} | waiting: average {minute.get('queue_avg', 0.0)}, "
            f"most {minute.get('queue_max', 0)}"
        )
    return "\n".join(out)
