"""Plan limits for CountVision: cameras and minutes of video analysis.

Cameras
- "In use" = cameras of devices that are not removed (revoked).
- New devices can only be paired while there is room on the plan.
- A device may still report more cameras than the plan allows (an edge config with 3 cameras on
  a 1-camera plan, or after a downgrade). Then the OLDEST cameras keep counting and the newest are
  paused: their state is shown, but their numbers are not stored. Upgrading (or removing another
  device) un-pauses them at the next upload. Nothing is deleted.
- Without a working plan (trial ended) nothing changes here: cameras that were counting keep
  sending numbers, so no data is lost while the customer decides.

Video analysis: minutes are counted in the month the analysis is started (a restart in the same
month is not counted twice).
"""

from __future__ import annotations

import logging
import math
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.services import audit, billing
from .. import live
from ..models import Camera, Device, VideoJob

log = logging.getLogger(__name__)


def month_key() -> str:
    return utcnow().strftime("%Y-%m")


def cameras_in_use(db: Session, org_id: uuid.UUID) -> list[Camera]:
    """Cameras that count against the plan, oldest first (the order decides who is paused)."""
    return list(
        db.scalars(
            select(Camera)
            .join(Device, Device.id == Camera.device_id)
            .where(Camera.org_id == org_id, Device.revoked_at.is_(None))
            .order_by(Camera.created_at, Camera.id)
        )
    )


def video_minutes_used(db: Session, org_id: uuid.UUID, month: str | None = None) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(VideoJob.billed_minutes), 0)).where(
                VideoJob.org_id == org_id, VideoJob.billed_month == (month or month_key())
            )
        )
        or 0
    )


def usage(db: Session, org_id: uuid.UUID) -> dict[str, int]:
    return {"cameras": len(cameras_in_use(db, org_id)), "video_minutes": video_minutes_used(db, org_id)}


def refresh_paused(db: Session, org_id: uuid.UUID) -> list[Camera]:
    """Pause the newest cameras above the plan's limit, un-pause the others. Returns the changed ones."""
    ent = billing.entitlement(db, org_id)
    if not ent.active:
        return []  # frozen: see the module docstring
    limit = ent.limit("cameras")
    changed: list[Camera] = []
    for rank, cam in enumerate(cameras_in_use(db, org_id)):
        want = 0 <= limit <= rank
        if cam.plan_paused != want:
            cam.plan_paused = want
            changed.append(cam)
            audit.record(
                db,
                "camera.plan_paused" if want else "camera.plan_resumed",
                actor=None,
                org_id=org_id,
                target_type="camera",
                target_id=cam.id,
                name=cam.name,
                limit=limit,
            )
    if changed:
        log.info(
            "Org %s: cameras %s by the plan limit (%d)",
            org_id,
            ", ".join(f"{c.name}={'paused' if c.plan_paused else 'counting'}" for c in changed),
            limit,
        )
        for device_id in {c.device_id for c in changed}:
            live.queue(db, org_id, {"type": "refresh", "device_id": str(device_id)})
    return changed


def check_can_pair(db: Session, org_id: uuid.UUID) -> None:
    """A new device brings at least one camera."""
    billing.check_limit(db, org_id, "cameras", adding=1)


def charge_video(db: Session, job: VideoJob) -> None:
    """Count the video's minutes against this month (once per month and job)."""
    month = month_key()
    if job.billed_month == month:
        return
    minutes = max(1, math.ceil((job.duration_s or 0) / 60))
    billing.check_limit(
        db, job.org_id, "video_minutes", adding=minutes, used=video_minutes_used(db, job.org_id)
    )
    job.billed_month, job.billed_minutes = month, minutes


def check_can_upload_video(db: Session, org_id: uuid.UUID) -> None:
    billing.check_limit(db, org_id, "video_minutes", adding=1, used=video_minutes_used(db, org_id))


billing.register_usage(usage)
billing.register_plan_changed(lambda db, org_id: refresh_paused(db, org_id))
