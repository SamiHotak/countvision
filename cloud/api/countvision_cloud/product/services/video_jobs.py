"""Video analysis jobs: upload a video file, draw lines and zones on its first frame, let the
worker count it with the SAME counting engine as the edge agent, get an annotated video, numbers,
a CSV and a PDF.

Upload: in pieces of up to 16 MB (``PUT .../upload?offset=``), so big files work through every
proxy and a broken upload can continue where it stopped.

Privacy (the one place where video reaches the cloud, and only because the customer uploads it):
- the uploaded video is deleted as soon as the analysis ends (also when it failed, after 1 day);
- the annotated video has people pixelated by default and is deleted after CV_VIDEO_KEEP_DAYS;
- the numbers stay until the job is deleted;
- the customer needs a legal basis for the recording; the web page says so.

Files live in ``<CV_DATA_DIR>/jobs/<job id>/``: input.<ext>, thumbnail.jpg, annotated.mp4,
report.pdf, counts.csv, work/ (the engine's SQLite database while running).
"""

from __future__ import annotations

import logging
import shutil
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError, Conflict, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit
from ...settings import get_settings
from ..models import VideoJob

log = logging.getLogger(__name__)

CHUNK_MAX = 16 * 1024 * 1024
CHUNK_SIZE = 8 * 1024 * 1024  # what the web page sends
MAX_STORED_JOBS = 50
ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg", ".ts", ".mts", ".wmv"}
ACTIVE = ("queued", "running")
FILES = {
    "thumbnail.jpg": "image/jpeg",
    "annotated.mp4": "video/mp4",
    "report.pdf": "application/pdf",
    "counts.csv": "text/csv; charset=utf-8",
}
BASE_TIME = datetime(2000, 1, 1, tzinfo=UTC)  # video second 0 = this time inside the engine
STALE_RUNNING = timedelta(minutes=30)


# --------------------------------------------------------------------------- files


def jobs_root() -> Path:
    return Path(get_settings().data_dir) / "jobs"


def job_dir(job: VideoJob | uuid.UUID) -> Path:
    return jobs_root() / str(job.id if isinstance(job, VideoJob) else job)


def input_path(job: VideoJob) -> Path:
    ext = Path(job.name).suffix.lower()
    return job_dir(job) / f"input{ext if ext in ALLOWED_EXT else '.bin'}"


def delete_input(job: VideoJob) -> None:
    input_path(job).unlink(missing_ok=True)


def delete_files(job: VideoJob) -> None:
    shutil.rmtree(job_dir(job), ignore_errors=True)


# --------------------------------------------------------------------------- CRUD


def get_job(db: Session, org: Organization, job_id: uuid.UUID) -> VideoJob:
    job = db.scalar(select(VideoJob).where(VideoJob.org_id == org.id, VideoJob.id == job_id))
    if job is None:
        raise NotFound("Video job not found.")
    return job


def list_jobs(db: Session, org: Organization) -> list[VideoJob]:
    return list(
        db.scalars(select(VideoJob).where(VideoJob.org_id == org.id).order_by(VideoJob.created_at.desc()))
    )


def create_job(db: Session, org: Organization, actor: User, name: str, size: int) -> VideoJob:
    settings = get_settings()
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise AppError(
            f"This file type is not supported. Use one of: {', '.join(sorted(ALLOWED_EXT))}.",
            code="bad_type",
            status=422,
            fields={"name": "Not a video file."},
        )
    if size <= 0 or size > settings.video_max_bytes:
        raise AppError(
            f"The file is too big. Max {settings.video_max_bytes / 1024**3:.1f} GB.",
            code="too_big",
            status=413,
        )
    stored = db.scalar(select(func.count(VideoJob.id)).where(VideoJob.org_id == org.id)) or 0
    if stored >= MAX_STORED_JOBS:
        raise Conflict(f"At most {MAX_STORED_JOBS} video jobs. Delete old ones first.", code="limit")
    job = VideoJob(
        org_id=org.id,
        created_by=actor.id,
        name=Path(name).name[:200],
        size_bytes=size,
        status="uploading",
        created_at=utcnow(),
    )
    db.add(job)
    db.flush()
    job_dir(job).mkdir(parents=True, exist_ok=True)
    audit.record(
        db,
        "video_job.created",
        actor=actor.id,
        org_id=org.id,
        target_type="video_job",
        target_id=job.id,
        name=job.name,
        size=size,
    )
    return job


def append_chunk(db: Session, job: VideoJob, offset: int, data: bytes) -> VideoJob:
    """Write one piece. ``offset`` must be where the file ends now (resume = ask the job first)."""
    if job.status != "uploading":
        raise Conflict("This upload is finished already.", code="not_uploading")
    if offset != job.received_bytes:
        raise Conflict(f"Wrong position: the server has {job.received_bytes} bytes.", code="bad_offset")
    if len(data) > CHUNK_MAX:
        raise AppError("Piece too big (max 16 MB).", code="chunk_too_big", status=413)
    if job.received_bytes + len(data) > job.size_bytes:
        raise AppError("More data than the announced file size.", code="too_much", status=422)
    path = input_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.truncate(offset)  # a half-written piece of a broken request is overwritten
        fh.seek(offset)
        fh.write(data)
    job.received_bytes = offset + len(data)
    job.updated_at = utcnow()
    return job


def complete_upload(db: Session, job: VideoJob) -> VideoJob:
    if job.status != "uploading":
        raise Conflict("This upload is finished already.", code="not_uploading")
    if job.received_bytes != job.size_bytes or input_path(job).stat().st_size != job.size_bytes:
        raise Conflict(
            f"The file is not complete ({job.received_bytes} of {job.size_bytes} bytes).", code="incomplete"
        )
    job.status = "probing"
    job.updated_at = utcnow()
    return job


def save_config(db: Session, job: VideoJob, actor: User, doc: dict[str, Any]) -> VideoJob:
    if job.status not in ("ready", "done", "failed", "cancelled"):
        raise Conflict("The video is not ready for lines and zones yet.", code="not_ready")
    if not doc.get("lines") and not doc.get("zones"):
        raise AppError("Draw at least one line or zone.", code="empty_config", status=422)
    job.config = doc
    audit.record(
        db,
        "video_job.configured",
        actor=actor.id,
        org_id=job.org_id,
        target_type="video_job",
        target_id=job.id,
        lines=len(doc.get("lines", [])),
        zones=len(doc.get("zones", [])),
    )
    return job


def start(db: Session, job: VideoJob, actor: User) -> VideoJob:
    if job.status not in ("ready", "failed", "cancelled", "done"):
        raise Conflict("This job cannot start now.", code="bad_state")
    if not job.config:
        raise AppError("Draw lines or zones first.", code="no_config", status=422)
    if not input_path(job).exists():
        raise Conflict(
            "The uploaded video was already deleted (it is removed after the analysis). "
            "Upload it again for a new analysis.",
            code="input_gone",
        )
    active = (
        db.scalar(
            select(func.count(VideoJob.id)).where(VideoJob.org_id == job.org_id, VideoJob.status.in_(ACTIVE))
        )
        or 0
    )
    if active >= get_settings().video_jobs_per_org:
        raise Conflict(
            f"At most {get_settings().video_jobs_per_org} videos are analysed at the same time. "
            "Wait until one is finished.",
            code="busy",
        )
    job.status, job.progress, job.error, job.result = "queued", 0.0, None, None
    job.cancel_requested = False
    job.started_at = job.finished_at = None
    job.updated_at = utcnow()
    audit.record(
        db, "video_job.started", actor=actor.id, org_id=job.org_id, target_type="video_job", target_id=job.id
    )
    return job


def cancel(db: Session, job: VideoJob, actor: User) -> VideoJob:
    if job.status == "queued":
        job.status, job.error = "cancelled", "Stopped before it started."
    elif job.status == "running":
        job.cancel_requested = True  # the worker sees it within 2 seconds
    else:
        raise Conflict("Only waiting or running jobs can be stopped.", code="bad_state")
    audit.record(
        db,
        "video_job.cancelled",
        actor=actor.id,
        org_id=job.org_id,
        target_type="video_job",
        target_id=job.id,
    )
    return job


def delete(db: Session, job: VideoJob, actor: User) -> None:
    if job.status == "running":
        raise Conflict("Stop the analysis first.", code="running")
    audit.record(
        db,
        "video_job.deleted",
        actor=actor.id,
        org_id=job.org_id,
        target_type="video_job",
        target_id=job.id,
        name=job.name,
    )
    delete_files(job)
    db.delete(job)
    db.flush()


def file_path(job: VideoJob, name: str) -> Path:
    if name not in FILES:
        raise NotFound("No such file.")
    path = job_dir(job) / name
    if not path.is_file():
        raise NotFound("This file does not exist (not made yet, or deleted after the keep time).")
    return path


# --------------------------------------------------------------------------- running


def dispatch(kind: str, job_id: uuid.UUID) -> None:
    """Hand a probe or a run to the worker (after the commit). kind: probe | run."""
    runner = get_settings().video_runner
    if runner == "off":
        return
    if runner == "thread":
        from . import video_worker

        fn = video_worker.probe if kind == "probe" else video_worker.run
        threading.Thread(target=fn, args=(job_id,), daemon=True, name=f"video-{kind}-{job_id}").start()
        return
    from ... import tasks

    if kind == "probe":
        tasks.probe_video.delay(str(job_id))
    else:
        tasks.run_video.apply_async(args=[str(job_id)], queue="video")


def job_out(job: VideoJob) -> dict[str, Any]:
    d = job_dir(job)
    files = {name: (d / name).is_file() for name in FILES}
    keep = get_settings().video_keep_days
    return {
        "id": job.id,
        "name": job.name,
        "status": job.status,
        "size_bytes": job.size_bytes,
        "received_bytes": job.received_bytes,
        "duration_s": job.duration_s,
        "fps": job.fps,
        "frame_count": job.frame_count,
        "width": job.width,
        "height": job.height,
        "config": job.config,
        "progress": round(job.progress, 4),
        "error": job.error,
        "result": job.result,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "files": files,
        "input_kept": input_path(job).exists(),
        "video_expires_at": (job.finished_at + timedelta(days=keep))
        if job.finished_at and files["annotated.mp4"]
        else None,
        "chunk_size": CHUNK_SIZE,
    }


# --------------------------------------------------------------------------- cleanup


def cleanup(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Hourly: delete what is no longer needed (data minimisation)."""
    now = now or utcnow()
    keep = timedelta(days=get_settings().video_keep_days)
    out = {"uploads": 0, "inputs": 0, "videos": 0, "stuck": 0}
    for job in db.scalars(select(VideoJob)):
        age = now - (job.updated_at or job.created_at)
        if job.status in ("uploading", "probing") and age > timedelta(days=1):
            delete_input(job)
            job.status, job.error = "failed", "The upload was not finished within a day; it was deleted."
            out["uploads"] += 1
        elif job.status == "ready" and age > keep:
            delete_input(job)
            job.status, job.error = "failed", "Not started within the keep time; the video was deleted."
            out["inputs"] += 1
        elif job.status in ("failed", "cancelled") and age > timedelta(days=1) and input_path(job).exists():
            delete_input(job)
            out["inputs"] += 1
        elif job.status == "running" and age > STALE_RUNNING:
            job.status, job.error = "failed", "The video worker stopped. Start the analysis again."
            out["stuck"] += 1
        if (
            job.status == "done"
            and job.finished_at
            and now - job.finished_at > keep
            and job.video_deleted_at is None
        ):
            (job_dir(job) / "annotated.mp4").unlink(missing_ok=True)
            job.video_deleted_at = now
            out["videos"] += 1
    return out
