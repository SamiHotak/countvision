"""Local SQLite buffer.

Everything the edge agent measures is written here first: events, 1-minute aggregates,
camera coverage and heartbeats. The buffer works offline. Rows have a ``sent`` flag so the
cloud uploader (phase 3) can send what is not yet sent and replay after an outage.
Only numbers are stored. There are no images and no coordinates.

The database uses WAL mode, so another process (the local web app, a report) can read while
the agent writes.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .types import Event

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    camera_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    name TEXT NOT NULL,
    track_id INTEGER,
    class_name TEXT,
    direction TEXT,
    enter_ts REAL,
    dwell_s REAL,
    speed_kmh REAL,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS idx_events_unsent ON events (sent, id);

CREATE TABLE IF NOT EXISTS line_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    line TEXT NOT NULL,
    class_name TEXT NOT NULL,          -- '*' = all classes together
    in_count INTEGER NOT NULL,
    out_count INTEGER NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (camera_id, window_start, line, class_name)
);
CREATE INDEX IF NOT EXISTS idx_line_counts_unsent ON line_counts (sent, id);

CREATE TABLE IF NOT EXISTS zone_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    zone TEXT NOT NULL,
    class_name TEXT NOT NULL,          -- '*' = all classes together
    sample_s REAL NOT NULL,            -- seconds of video measured in this minute
    occ_avg REAL NOT NULL,
    occ_max INTEGER NOT NULL,
    occ_last INTEGER NOT NULL,
    queue_avg REAL NOT NULL,
    queue_max INTEGER NOT NULL,
    visits INTEGER NOT NULL,           -- visits that ended in this minute
    dwell_sum_s REAL NOT NULL,
    dwell_max_s REAL NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (camera_id, window_start, zone, class_name)
);
CREATE INDEX IF NOT EXISTS idx_zone_stats_unsent ON zone_stats (sent, id);

CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    frames INTEGER NOT NULL,
    seconds REAL NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (camera_id, window_start)
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    camera_id TEXT NOT NULL,
    fps REAL,
    proc_ms REAL,
    frames_processed INTEGER,
    frames_skipped INTEGER,
    frames_dropped INTEGER,
    cpu_pct REAL,
    mem_pct REAL,
    gpu_util_pct REAL,
    gpu_mem_mb REAL,
    connected INTEGER,
    reconnects INTEGER,
    frame_age_s REAL,
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats (ts);
"""

_TABLES = ("events", "line_counts", "zone_stats", "coverage", "heartbeats")
_TIME_COLUMN = {
    "events": "ts",
    "line_counts": "window_start",
    "zone_stats": "window_start",
    "coverage": "window_start",
    "heartbeats": "ts",
}


@dataclass
class LineCountRow:
    camera_id: str
    window_start: int
    line: str
    class_name: str
    in_count: int
    out_count: int


@dataclass
class ZoneStatRow:
    camera_id: str
    window_start: int
    zone: str
    class_name: str
    sample_s: float
    occ_avg: float
    occ_max: int
    occ_last: int
    queue_avg: float
    queue_max: int
    visits: int
    dwell_sum_s: float
    dwell_max_s: float


@dataclass
class CoverageRow:
    camera_id: str
    window_start: int
    frames: int
    seconds: float


class SqliteBuffer:
    """Thread-safe SQLite store for everything the agent measures."""

    def __init__(
        self,
        path: str | Path,
        *,
        retention_days: int = 30,
        heartbeat_retention_hours: int = 48,
    ) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self.heartbeat_retention_hours = heartbeat_retention_hours
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._db.commit()

    # -- writing ------------------------------------------------------------------------

    def add_events(self, events: Iterable[Event]) -> None:
        rows = [
            (
                e.event_id, e.camera_id, e.kind, e.ts, e.name, e.track_id, e.class_name,
                e.direction, e.enter_ts, e.dwell_s, e.speed_kmh,
            )
            for e in events
        ]
        if not rows:
            return
        with self._lock:
            self._db.executemany(
                "INSERT OR IGNORE INTO events (event_id, camera_id, kind, ts, name, track_id, "
                "class_name, direction, enter_ts, dwell_s, speed_kmh) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._db.commit()

    def add_line_counts(self, rows: Iterable[LineCountRow]) -> None:
        """Add counts. A row for a minute that already exists is ADDED (restart-safe)."""
        data = [
            (r.camera_id, int(r.window_start), r.line, r.class_name, r.in_count, r.out_count)
            for r in rows
        ]
        if not data:
            return
        with self._lock:
            self._db.executemany(
                "INSERT INTO line_counts (camera_id, window_start, line, class_name, "
                "in_count, out_count) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT (camera_id, window_start, line, class_name) DO UPDATE SET "
                "in_count = in_count + excluded.in_count, "
                "out_count = out_count + excluded.out_count, sent = 0",
                data,
            )
            self._db.commit()

    def add_zone_stats(self, rows: Iterable[ZoneStatRow]) -> None:
        """Add zone statistics. An existing minute is merged, weighted by measured seconds."""
        with self._lock:
            for r in rows:
                key = (r.camera_id, int(r.window_start), r.zone, r.class_name)
                old = self._db.execute(
                    "SELECT * FROM zone_stats WHERE camera_id=? AND window_start=? "
                    "AND zone=? AND class_name=?",
                    key,
                ).fetchone()
                if old is None:
                    self._db.execute(
                        "INSERT INTO zone_stats (camera_id, window_start, zone, class_name, "
                        "sample_s, occ_avg, occ_max, occ_last, queue_avg, queue_max, visits, "
                        "dwell_sum_s, dwell_max_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*key, r.sample_s, r.occ_avg, r.occ_max, r.occ_last, r.queue_avg,
                         r.queue_max, r.visits, r.dwell_sum_s, r.dwell_max_s),
                    )
                    continue
                total = old["sample_s"] + r.sample_s
                occ_sum = old["occ_avg"] * old["sample_s"] + r.occ_avg * r.sample_s
                queue_sum = old["queue_avg"] * old["sample_s"] + r.queue_avg * r.sample_s
                occ_avg = occ_sum / total if total else 0.0
                queue_avg = queue_sum / total if total else 0.0
                self._db.execute(
                    "UPDATE zone_stats SET sample_s=?, occ_avg=?, occ_max=?, occ_last=?, "
                    "queue_avg=?, queue_max=?, visits=?, dwell_sum_s=?, dwell_max_s=?, sent=0 "
                    "WHERE id=?",
                    (total, occ_avg, max(old["occ_max"], r.occ_max), r.occ_last, queue_avg,
                     max(old["queue_max"], r.queue_max), old["visits"] + r.visits,
                     old["dwell_sum_s"] + r.dwell_sum_s, max(old["dwell_max_s"], r.dwell_max_s),
                     old["id"]),
                )
            self._db.commit()

    def add_coverage(self, row: CoverageRow) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO coverage (camera_id, window_start, frames, seconds) VALUES (?,?,?,?) "
                "ON CONFLICT (camera_id, window_start) DO UPDATE SET "
                "frames = frames + excluded.frames, seconds = seconds + excluded.seconds, sent = 0",
                (row.camera_id, int(row.window_start), row.frames, row.seconds),
            )
            self._db.commit()

    def add_heartbeat(self, beat: dict) -> None:
        columns = (
            "ts", "camera_id", "fps", "proc_ms", "frames_processed", "frames_skipped",
            "frames_dropped", "cpu_pct", "mem_pct", "gpu_util_pct", "gpu_mem_mb", "connected",
            "reconnects", "frame_age_s",
        )
        values = [beat.get(c) for c in columns]
        with self._lock:
            self._db.execute(
                f"INSERT INTO heartbeats ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' * len(columns))})",
                values,
            )
            self._db.commit()

    # -- upload support (cloud.py) -------------------------------------------------------

    def fetch_unsent(self, table: str, limit: int = 500) -> list[dict]:
        """Rows that have not been uploaded yet, oldest first."""
        self._check_table(table)
        with self._lock:
            cursor = self._db.execute(
                f"SELECT * FROM {table} WHERE sent = 0 ORDER BY id LIMIT ?", (limit,)  # noqa: S608
            )
            return [dict(row) for row in cursor.fetchall()]

    # Columns that change when a minute row is updated (add_* merges new data into a row and
    # sets sent=0). A row is only marked as sent if these still have the uploaded values, so an
    # update that happens DURING an upload is sent again next time instead of being lost.
    _CHANGING = {
        "line_counts": ("in_count", "out_count"),
        "zone_stats": ("sample_s", "visits", "occ_last"),
        "coverage": ("frames", "seconds"),
    }

    def mark_sent_rows(self, table: str, rows: Sequence[dict]) -> int:
        """Mark uploaded rows as sent, unless they changed meanwhile. Returns rows marked."""
        self._check_table(table)
        if not rows:
            return 0
        columns = self._CHANGING.get(table, ())
        where = " AND ".join(["id = ?"] + [f"{c} = ?" for c in columns])
        params = [(r["id"], *[r[c] for c in columns]) for r in rows]
        with self._lock:
            before = self._db.total_changes
            self._db.executemany(f"UPDATE {table} SET sent = 1 WHERE {where}", params)  # noqa: S608
            self._db.commit()
            return self._db.total_changes - before

    def mark_sent_upto(self, table: str, max_id: int) -> None:
        """Mark every row up to an id as sent (heartbeats: only the newest one is uploaded)."""
        self._check_table(table)
        with self._lock:
            self._db.execute(f"UPDATE {table} SET sent = 1 WHERE sent = 0 AND id <= ?", (max_id,))  # noqa: S608
            self._db.commit()

    def count_unsent(self, tables: Sequence[str] = ("events", "line_counts", "zone_stats", "coverage"),
                     ) -> int:
        """Rows waiting for upload (heartbeats are not counted: only the newest is sent)."""
        total = 0
        with self._lock:
            for table in tables:
                self._check_table(table)
                total += self._db.execute(f"SELECT COUNT(*) FROM {table} WHERE sent = 0").fetchone()[0]  # noqa: S608
        return total

    def mark_sent(self, table: str, ids: Sequence[int]) -> None:
        self._check_table(table)
        if not ids:
            return
        with self._lock:
            self._db.executemany(
                f"UPDATE {table} SET sent = 1 WHERE id = ?", [(i,) for i in ids]  # noqa: S608
            )
            self._db.commit()

    # -- reading ------------------------------------------------------------------------

    def query(
        self,
        table: str,
        *,
        camera_id: str | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[dict]:
        """Rows of ``table`` for a camera and a time range [start, end), oldest first."""
        self._check_table(table)
        column = _TIME_COLUMN[table]
        where, params = [], []
        if camera_id is not None:
            where.append("camera_id = ?")
            params.append(camera_id)
        if start is not None:
            where.append(f"{column} >= ?")
            params.append(start)
        if end is not None:
            where.append(f"{column} < ?")
            params.append(end)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        sql = f"SELECT * FROM {table}{clause} ORDER BY {column}, id"  # noqa: S608
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def latest_ts(self, camera_id: str) -> float | None:
        """The newest time that has data for a camera (event time or end of a minute row)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(t) FROM ("
                "SELECT MAX(ts) AS t FROM events WHERE camera_id = ? "
                "UNION ALL SELECT MAX(window_start) + 60 FROM coverage WHERE camera_id = ?)",
                (camera_id, camera_id),
            ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def latest_heartbeat(self, camera_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM heartbeats WHERE camera_id = ? ORDER BY id DESC LIMIT 1",
                (camera_id,),
            ).fetchone()
        return dict(row) if row else None

    # -- housekeeping -------------------------------------------------------------------

    def purge(self, now: float | None = None) -> dict[str, int]:
        """Delete data older than the retention settings. Returns rows deleted per table."""
        current = time.time() if now is None else now
        cutoff = current - self.retention_days * 86400
        heartbeat_cutoff = current - self.heartbeat_retention_hours * 3600
        deleted: dict[str, int] = {}
        with self._lock:
            for table in _TABLES:
                limit = heartbeat_cutoff if table == "heartbeats" else cutoff
                cursor = self._db.execute(
                    f"DELETE FROM {table} WHERE {_TIME_COLUMN[table]} < ?", (limit,)  # noqa: S608
                )
                deleted[table] = cursor.rowcount
            self._db.commit()
        if any(deleted.values()):
            log.info("Retention: deleted old rows %s", deleted)
        return deleted

    def close(self) -> None:
        with self._lock:
            try:
                self._db.commit()
                self._db.close()
            except sqlite3.ProgrammingError:
                pass  # already closed

    @staticmethod
    def _check_table(table: str) -> None:
        if table not in _TABLES:
            raise ValueError(f"unknown table: {table}")
