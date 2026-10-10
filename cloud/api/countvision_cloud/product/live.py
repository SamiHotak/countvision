"""Live messages (Redis pub/sub) and the per-device "something changed" counter.

* ``queue(db, org_id, message)``: publish to the organization's live channel AFTER the database
  transaction commits (a browser never sees a number the database does not have).
* ``bump_device(device_id)``: increase the device's revision. A device waiting in
  GET /api/device/poll sees the change within half a second (new config, snapshot request).

Live messages contain only numbers and names, never pictures.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from ..redis_client import get_redis

log = logging.getLogger(__name__)

_OUTBOX = "cv_live"


def org_channel(org_id: uuid.UUID | str) -> str:
    return f"cv:live:{org_id}"


def device_rev_key(device_id: uuid.UUID | str) -> str:
    return f"cv:dev:{device_id}:rev"


def device_snaps_key(device_id: uuid.UUID | str) -> str:
    return f"cv:dev:{device_id}:snapreq"


def queue(db: Session, org_id: uuid.UUID, message: dict[str, Any]) -> None:
    db.info.setdefault(_OUTBOX, []).append((str(org_id), message))


def publish(org_id: uuid.UUID | str, message: dict[str, Any]) -> None:
    try:
        get_redis().publish(org_channel(org_id), json.dumps(message, default=str, separators=(",", ":")))
    except Exception:  # live updates are a bonus: never fail a request because of them
        log.warning("Live publish failed", exc_info=True)


def bump_device(device_id: uuid.UUID | str) -> int:
    try:
        return int(get_redis().incr(device_rev_key(device_id)))
    except Exception:
        log.warning("Could not bump device revision", exc_info=True)
        return 0


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    for org_id, message in session.info.pop(_OUTBOX, []):
        publish(org_id, message)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_OUTBOX, None)
