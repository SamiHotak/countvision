"""Ingest: the edge agent uploads batches of numbers.

Idempotent by design (the agent re-sends after network problems):
- minute rows (line_counts, zone_stats, coverage) carry the FULL value of that minute, so the
  cloud stores them with INSERT ... ON CONFLICT DO UPDATE (newest value wins);
- events have a unique id, so a repeated event is ignored (ON CONFLICT DO NOTHING).
Sending the same batch twice therefore changes nothing.

Rows with impossible times (more than 1 day in the future or older than 400 days) are
rejected and counted, the rest of the batch is stored.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError
from ..models import Camera, CountEvent, Coverage, Device, IngestBatch, LineCount, ZoneStat
from ..schemas import IngestIn, IngestOut

log = logging.getLogger(__name__)

MAX_CAMERAS_PER_DEVICE = 64
MAX_FUTURE_S = 86400
MAX_AGE_S = 400 * 86400
_STATUS_KEYS = ("version", "detector", "privacy", "upload", "started", "device_id")


def _ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, tz=UTC)


def _ok_time(value: float, now: float) -> bool:
    return now - MAX_AGE_S <= value <= now + MAX_FUTURE_S


def _cameras(db: Session, device: Device, payload: IngestIn) -> dict[str, Camera]:
    """Existing cameras of the device, plus new ones for every id that appears in the batch."""
    names = {c.id: c.name for c in payload.cameras}
    ids: set[str] = set(names)
    for rows in (payload.line_counts, payload.zone_stats, payload.coverage, payload.events,
                 payload.heartbeats):
        ids.update(r.camera_id for r in rows)
    existing = {c.edge_camera_id: c for c in db.scalars(select(Camera).where(Camera.device_id == device.id))}
    missing = sorted(ids - set(existing))
    if len(existing) + len(missing) > MAX_CAMERAS_PER_DEVICE:
        raise AppError(f"A device can report at most {MAX_CAMERAS_PER_DEVICE} cameras.", code="limit",
                       status=422)
    for edge_id in missing:
        cam = Camera(org_id=device.org_id, device_id=device.id, edge_camera_id=edge_id,
                     name=names.get(edge_id) or edge_id)
        db.add(cam)
        existing[edge_id] = cam
    if missing:
        db.flush()
        log.info("Device %s: new cameras %s", device.id, ", ".join(missing))
    return existing


CHUNK = 1000  # rows per INSERT (PostgreSQL allows at most 65535 parameters per statement)


def _dedupe(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """Last row wins when a batch contains the same key twice (one INSERT may not hit a row twice)."""
    return list({tuple(r[k] for k in keys): r for r in rows}.values())


def _upsert(db: Session, model, rows: list[dict], keys: tuple[str, ...], update: tuple[str, ...]) -> int:
    rows = _dedupe(rows, keys)
    for i in range(0, len(rows), CHUNK):
        stmt = insert(model).values(rows[i:i + CHUNK])
        if update:
            stmt = stmt.on_conflict_do_update(index_elements=list(keys),
                                              set_={c: getattr(stmt.excluded, c) for c in update})
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
        db.execute(stmt)
    return len(rows)


def ingest(db: Session, device: Device, payload: IngestIn) -> IngestOut:
    now_dt = utcnow()
    now = now_dt.timestamp()
    cams = _cameras(db, device, payload)
    rejected = 0
    accepted: dict[str, int] = {}
    newest: float | None = None

    def keep(value: float) -> bool:
        nonlocal rejected, newest
        if _ok_time(value, now):
            newest = value if newest is None else max(newest, value)
            return True
        rejected += 1
        return False

    # -- minute rows: newest value wins ----------------------------------------------------------
    rows = [
        {"camera_id": cams[r.camera_id].id, "window_start": _ts(r.window_start), "line": r.line,
         "class_name": r.class_name, "in_count": r.in_count, "out_count": r.out_count, "received_at": now_dt}
        for r in payload.line_counts if keep(r.window_start + 60)
    ]
    accepted["line_counts"] = _upsert(db, LineCount, rows,
                                      ("camera_id", "line", "class_name", "window_start"),
                                      ("in_count", "out_count", "received_at"))

    zone_fields = ("sample_s", "occ_avg", "occ_max", "occ_last", "queue_avg", "queue_max", "visits",
                   "dwell_sum_s", "dwell_max_s")
    rows = [
        {"camera_id": cams[r.camera_id].id, "window_start": _ts(r.window_start), "zone": r.zone,
         "class_name": r.class_name, "received_at": now_dt, **{f: getattr(r, f) for f in zone_fields}}
        for r in payload.zone_stats if keep(r.window_start + 60)
    ]
    accepted["zone_stats"] = _upsert(db, ZoneStat, rows, ("camera_id", "zone", "class_name", "window_start"),
                                     (*zone_fields, "received_at"))

    rows = [
        {"camera_id": cams[r.camera_id].id, "window_start": _ts(r.window_start), "frames": r.frames,
         "seconds": r.seconds}
        for r in payload.coverage if keep(r.window_start + 60)
    ]
    accepted["coverage"] = _upsert(db, Coverage, rows, ("camera_id", "window_start"), ("frames", "seconds"))

    # -- events: once ---------------------------------------------------------------------------
    rows = [
        {"camera_id": cams[r.camera_id].id, "ts": _ts(r.ts), "event_id": r.event_id, "kind": r.kind,
         "name": r.name, "class_name": r.class_name or None, "direction": r.direction,
         "dwell_s": r.dwell_s, "speed_kmh": r.speed_kmh}
        for r in payload.events if keep(r.ts)
    ]
    accepted["events"] = _upsert(db, CountEvent, rows, ("camera_id", "event_id", "ts"), ())

    # -- live state: status list + newest heartbeat per camera -----------------------------------
    for status in payload.cameras:
        cam = cams[status.id]
        if status.name and not cam.name_locked:
            cam.name = status.name
        cam.state = status.state
        cam.connected = status.connected
        cam.fps = status.fps
        cam.reconnects = status.reconnects
        cam.alert = status.alert
        cam.lines = list(status.lines)
        cam.zones = list(status.zones)
        cam.last_seen_at = now_dt
    newest_beat: dict[str, Any] = {}
    for beat in payload.heartbeats:
        if beat.camera_id not in newest_beat or beat.ts > newest_beat[beat.camera_id].ts:
            newest_beat[beat.camera_id] = beat
    for edge_id, beat in newest_beat.items():
        cam = cams[edge_id]
        if not payload.cameras:  # older agents without the status list
            cam.fps = beat.fps
            cam.connected = beat.connected
            cam.reconnects = beat.reconnects
        cam.last_seen_at = now_dt
    accepted["heartbeats"] = len(payload.heartbeats)

    # -- device ----------------------------------------------------------------------------------
    device.last_seen_at = now_dt
    if payload.agent_version:
        device.agent_version = payload.agent_version
    if payload.status is not None:
        device.status = {k: payload.status[k] for k in _STATUS_KEYS if k in payload.status}
    if newest is not None:
        newest_dt = _ts(min(newest, now))
        if device.last_data_at is None or newest_dt > device.last_data_at:
            device.last_data_at = newest_dt

    total = sum(accepted.values())
    result = db.execute(
        insert(IngestBatch).values(device_id=device.id, batch_id=payload.batch_id, rows=total,
                                   rejected=rejected, received_at=now_dt)
        .on_conflict_do_nothing(index_elements=["device_id", "batch_id"]).returning(IngestBatch.id)
    )
    duplicate = result.first() is None
    if rejected:
        log.warning("Device %s batch %s: %d rows with impossible times rejected", device.id,
                    payload.batch_id, rejected)
    return IngestOut(accepted=accepted, rejected=rejected, server_time=time.time(), duplicate_batch=duplicate)
