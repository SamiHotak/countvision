"""Reports from the SQLite buffer: a text summary, JSON and CSV export."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .errors import CountVisionError
from .storage import SqliteBuffer


def _iso(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")  # noqa: UP017


def open_existing(db_path: str | Path) -> SqliteBuffer:
    """Open a buffer that must already exist (a report must never create an empty database)."""
    path = Path(db_path)
    if not path.is_file():
        raise CountVisionError(f"Database not found: {path}")
    return SqliteBuffer(path)


def build_report(
    buffer: SqliteBuffer,
    camera_id: str | None = None,
    start: float | None = None,
    end: float | None = None,
    *,
    line_names: Iterable[str] = (),
) -> dict:
    """Totals per camera, line and zone for a time range ([start, end) in UTC epoch seconds).

    ``line_names`` (needs ``camera_id``): lines that must appear even with zero crossings, so
    a quiet line prints "IN 0, OUT 0" instead of nothing.
    """
    lines = buffer.query("line_counts", camera_id=camera_id, start=start, end=end)
    zones = buffer.query("zone_stats", camera_id=camera_id, start=start, end=end)
    coverage = buffer.query("coverage", camera_id=camera_id, start=start, end=end)
    events = buffer.query("events", camera_id=camera_id, start=start, end=end)

    cameras: dict[str, dict] = {}

    def camera(cam: str) -> dict:
        return cameras.setdefault(
            cam,
            {"first_ts": None, "last_ts": None, "measured_s": 0.0, "events": 0,
             "lines": {}, "zones": {}},
        )

    for row in coverage:
        c = camera(row["camera_id"])
        c["measured_s"] += row["seconds"]
        t0, t1 = row["window_start"], row["window_start"] + 60
        c["first_ts"] = t0 if c["first_ts"] is None else min(c["first_ts"], t0)
        c["last_ts"] = t1 if c["last_ts"] is None else max(c["last_ts"], t1)
    for row in events:
        camera(row["camera_id"])["events"] += 1
    for row in lines:
        entry = camera(row["camera_id"])["lines"].setdefault(
            row["line"], {"in": 0, "out": 0, "by_class": {}}
        )
        if row["class_name"] == "*":
            entry["in"] += row["in_count"]
            entry["out"] += row["out_count"]
        else:
            cls = entry["by_class"].setdefault(row["class_name"], {"in": 0, "out": 0})
            cls["in"] += row["in_count"]
            cls["out"] += row["out_count"]
    for row in zones:
        if row["class_name"] != "*":
            continue
        entry = camera(row["camera_id"])["zones"].setdefault(
            row["zone"],
            {"sample_s": 0.0, "occ_weighted": 0.0, "occ_max": 0, "queue_weighted": 0.0,
             "queue_max": 0, "visits": 0, "dwell_sum_s": 0.0, "dwell_max_s": 0.0},
        )
        entry["sample_s"] += row["sample_s"]
        entry["occ_weighted"] += row["occ_avg"] * row["sample_s"]
        entry["queue_weighted"] += row["queue_avg"] * row["sample_s"]
        entry["occ_max"] = max(entry["occ_max"], row["occ_max"])
        entry["queue_max"] = max(entry["queue_max"], row["queue_max"])
        entry["visits"] += row["visits"]
        entry["dwell_sum_s"] += row["dwell_sum_s"]
        entry["dwell_max_s"] = max(entry["dwell_max_s"], row["dwell_max_s"])
    if camera_id is not None:
        for name in line_names:
            camera(camera_id)["lines"].setdefault(name, {"in": 0, "out": 0, "by_class": {}})
    for cam in cameras.values():
        for zone in cam["zones"].values():
            seconds = zone.pop("sample_s")
            zone["occupancy_avg"] = round(zone.pop("occ_weighted") / seconds, 2) if seconds else 0.0
            zone["queue_avg"] = round(zone.pop("queue_weighted") / seconds, 2) if seconds else 0.0
            zone["occupancy_max"] = zone.pop("occ_max")
            zone["queue_max"] = zone["queue_max"]
            zone["dwell_avg_s"] = (
                round(zone["dwell_sum_s"] / zone["visits"], 1) if zone["visits"] else 0.0
            )
            zone["dwell_max_s"] = round(zone["dwell_max_s"], 1)
            zone.pop("dwell_sum_s")
        cam["measured_s"] = round(cam["measured_s"], 1)
    return {"cameras": cameras}


def format_report(report: dict) -> str:
    """Human-readable text for the console."""
    if not report["cameras"]:
        return "No data in the database for this range."
    out: list[str] = []
    for cam_id, cam in sorted(report["cameras"].items()):
        out.append(f"Camera {cam_id}: {_iso(cam['first_ts'])} to {_iso(cam['last_ts'])}")
        out.append(f"  measured video time: {cam['measured_s']:.0f} s, events: {cam['events']}")
        for name, line in sorted(cam["lines"].items()):
            out.append(f"  Line '{name}': IN {line['in']}, OUT {line['out']}")
            for cls, counts in sorted(line["by_class"].items()):
                out.append(f"      {cls}: IN {counts['in']}, OUT {counts['out']}")
        for name, zone in sorted(cam["zones"].items()):
            out.append(
                f"  Zone '{name}': occupancy avg {zone['occupancy_avg']}, max {zone['occupancy_max']}"
                f" | visits {zone['visits']}, dwell avg {zone['dwell_avg_s']} s, max {zone['dwell_max_s']} s"
                f" | queue avg {zone['queue_avg']}, max {zone['queue_max']}"
            )
    return "\n".join(out)


def export_csv(
    buffer: SqliteBuffer,
    out_dir: str | Path,
    camera_id: str | None = None,
    start: float | None = None,
    end: float | None = None,
) -> list[Path]:
    """Write events.csv, line_counts.csv, zone_stats.csv and coverage.csv. Returns the paths."""
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for table, time_column, label in (
        ("events", "ts", "ts_utc"),
        ("line_counts", "window_start", "minute_utc"),
        ("zone_stats", "window_start", "minute_utc"),
        ("coverage", "window_start", "minute_utc"),
    ):
        rows = buffer.query(table, camera_id=camera_id, start=start, end=end)
        path = folder / f"{table}.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            if rows:
                columns = [c for c in rows[0] if c not in ("id", "sent")]
                writer = csv.writer(handle)
                writer.writerow([label, *columns])
                for row in rows:
                    stamp = datetime.fromtimestamp(row[time_column], tz=timezone.utc)  # noqa: UP017
                    writer.writerow([stamp.strftime("%Y-%m-%d %H:%M:%S"), *(row[c] for c in columns)])
            else:
                handle.write("")
        written.append(path)
    return written
