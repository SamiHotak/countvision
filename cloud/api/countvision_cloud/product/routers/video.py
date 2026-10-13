"""/api/orgs/{org_id}/video-jobs: upload a video file, draw lines and zones, analyse it.

Members upload, configure, start, stop and delete. Everybody in the organization sees the jobs and
downloads the results.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ...db import get_db, session_factory
from ...saas.deps import OrgContext, org_access, require_plan
from ...saas.errors import AppError
from ...saas.models import Role
from ...saas.schemas import OkOut
from ..schemas import VideoConfigIn, VideoJobIn
from ..services import video_jobs

router = APIRouter(prefix="/api/orgs/{org_id}/video-jobs", tags=["video analysis"],
                   dependencies=[Depends(require_plan)])  # 402 without a working plan


@router.get("")
def list_jobs(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [video_jobs.job_out(j) for j in video_jobs.list_jobs(db, ctx.org)]


@router.post("", status_code=201)
def create_job(
    body: VideoJobIn, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = video_jobs.create_job(db, ctx.org, ctx.user, body.name, body.size_bytes)
    db.commit()
    return video_jobs.job_out(job)


@router.get("/{job_id}")
def get_job(
    job_id: uuid.UUID, ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)
) -> dict:
    return video_jobs.job_out(video_jobs.get_job(db, ctx.org, job_id))


@router.put("/{job_id}/upload")
async def upload_chunk(
    job_id: uuid.UUID, offset: int, request: Request, ctx: OrgContext = Depends(org_access(Role.MEMBER))
) -> dict[str, Any]:
    """One piece of the file (raw bytes, max 16 MB) at ``offset``."""
    size = int(request.headers.get("content-length") or 0)
    if size > video_jobs.CHUNK_MAX:
        raise AppError("Piece too big (max 16 MB).", code="chunk_too_big", status=413)
    data = await request.body()

    def write() -> dict[str, Any]:
        with session_factory()() as db:
            job = video_jobs.get_job(db, ctx.org, job_id)
            video_jobs.append_chunk(db, job, offset, data)
            db.commit()
            return {"received_bytes": job.received_bytes, "size_bytes": job.size_bytes}

    return await run_in_threadpool(write)


@router.post("/{job_id}/complete")
def complete(
    job_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = video_jobs.complete_upload(db, video_jobs.get_job(db, ctx.org, job_id))
    db.commit()
    video_jobs.dispatch("probe", job.id)
    return video_jobs.job_out(job)


@router.put("/{job_id}/config")
def save_config(
    job_id: uuid.UUID,
    body: VideoConfigIn,
    ctx: OrgContext = Depends(org_access(Role.MEMBER)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job = video_jobs.save_config(db, video_jobs.get_job(db, ctx.org, job_id), ctx.user, body.model_dump())
    db.commit()
    return video_jobs.job_out(job)


@router.post("/{job_id}/start")
def start(
    job_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = video_jobs.start(db, video_jobs.get_job(db, ctx.org, job_id), ctx.user)
    db.commit()
    video_jobs.dispatch("run", job.id)
    return video_jobs.job_out(job)


@router.post("/{job_id}/cancel")
def cancel(
    job_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = video_jobs.cancel(db, video_jobs.get_job(db, ctx.org, job_id), ctx.user)
    db.commit()
    return video_jobs.job_out(job)


@router.delete("/{job_id}", response_model=OkOut)
def delete(
    job_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> OkOut:
    video_jobs.delete(db, video_jobs.get_job(db, ctx.org, job_id), ctx.user)
    db.commit()
    return OkOut(message="Video job and its files deleted.")


@router.get("/{job_id}/files/{name}")
def download(
    job_id: uuid.UUID,
    name: str,
    download: bool = False,
    ctx: OrgContext = Depends(org_access()),
    db: Session = Depends(get_db),
) -> FileResponse:
    job = video_jobs.get_job(db, ctx.org, job_id)
    path = video_jobs.file_path(job, name)
    stem = job.name.rsplit(".", 1)[0]
    safe = (
        "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in stem)[:60] or "video"
    )
    filename = f"countvision_{safe}_{name}"
    inline = name in ("annotated.mp4", "thumbnail.jpg") and not download  # play / show in the page
    return FileResponse(
        path,
        media_type=video_jobs.FILES[name],
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, max-age=300"},
    )
