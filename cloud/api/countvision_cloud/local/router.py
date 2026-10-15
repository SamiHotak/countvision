"""HTTP endpoints of the local edition, plus GET /api/edition for both editions.

Public:  GET /api/edition, POST /api/local/setup (only while no account exists)
Org:     /api/orgs/{org_id}/licence (GET members, PUT admins, DELETE owners)
         /api/orgs/{org_id}/backups ... (owners only: a backup contains everything)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.orm import Session

from .. import __version__
from ..db import get_db, session_factory
from ..saas.cookies import clear_session_cookie, set_session_cookie
from ..saas.deps import OrgContext, org_access
from ..saas.errors import AppError
from ..saas.models import Role
from ..saas.routers.me import me_out
from ..saas.services import audit, auth, ratelimit
from ..settings import get_settings
from . import backup, licence, setup

log = logging.getLogger(__name__)

public_router = APIRouter(tags=["edition"])
router = APIRouter(tags=["local edition"])


@public_router.get("/api/edition")
def edition(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Which edition runs, so the web app can show the right pages (no login needed)."""
    s = get_settings()
    if s.is_local:
        return {**setup.status(db), "google": False, "signup": False, "telegram": False, "billing": "licence"}
    return {"edition": "cloud", "version": __version__, "needs_setup": False,
            "email": s.email_backend != "off",
            "google": s.google_enabled, "signup": True, "telegram": True,
            "billing": "stripe" if s.billing_enabled else "off"}


# --- first start ------------------------------------------------------------------------------------


class SetupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)
    business: str = Field(min_length=1, max_length=120)
    site_name: str = Field(min_length=1, max_length=120)
    timezone: str = "Europe/Berlin"

    @field_validator("name", "business", "site_name")
    @classmethod
    def _clean(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Please fill this in")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        from ..product.schemas import SiteIn

        return SiteIn(name="x", timezone=value).timezone


@router.post("/api/local/setup", status_code=201)
def run_setup(body: SetupIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    """The wizard's first step: owner account, business, site, the built-in device. Logs in."""
    ratelimit.limit("setup-ip", ratelimit.client_ip(request), 20, 3600)
    user, org, site, device = setup.run(db, name=body.name, email=str(body.email), password=body.password,
                                        business=body.business, site_name=body.site_name,
                                        timezone=body.timezone)
    token = auth.start_session(db, user)
    db.commit()
    set_session_cookie(response, token)
    log.info("CountVision Local set up for %s (site %s)", org.name, site.name)
    return {"me": me_out(db, user).model_dump(mode="json"), "org_id": str(org.id), "site_id": str(site.id),
            "device_id": str(device.id)}


# --- licence ----------------------------------------------------------------------------------------


class LicenceIn(BaseModel):
    key: str = Field(min_length=10, max_length=4000)


@router.get("/api/orgs/{org_id}/licence")
def get_licence(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> dict:
    out = licence.summary(db, ctx.org.id)
    db.commit()  # installed_at may have been created
    return out


@router.put("/api/orgs/{org_id}/licence")
def put_licence(body: LicenceIn, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                db: Session = Depends(get_db)) -> dict:
    ratelimit.limit("licence-user", str(ctx.user.id), 20, 3600, "Too many tries. Wait an hour.")
    lic = licence.install(db, ctx.org, ctx.user, body.key)
    db.commit()
    return {**licence.summary(db, ctx.org.id), "message": f"Licence added: {lic.cameras} cameras."}


@router.delete("/api/orgs/{org_id}/licence")
def delete_licence(ctx: OrgContext = Depends(org_access(Role.OWNER)), db: Session = Depends(get_db)) -> dict:
    licence.remove(db, ctx.org, ctx.user)
    db.commit()
    return licence.summary(db, ctx.org.id)


# --- backups ----------------------------------------------------------------------------------------


class BackupIn(BaseModel):
    folder: str | None = Field(default=None, max_length=400)


class RestoreIn(BaseModel):
    folder: str | None = Field(default=None, max_length=400)
    confirm: str = Field(max_length=200)  # the business name, typed by the owner


@router.get("/api/orgs/{org_id}/backups")
def list_backups(folder: str | None = Query(None, max_length=400),
                 ctx: OrgContext = Depends(org_access(Role.OWNER))) -> dict:
    base = backup.folder(folder)
    return {"folder": str(base), "default_folder": str(get_settings().backups_path),
            "free_bytes": backup.free_space(folder),
            "backups": [b.as_dict() for b in backup.list_backups(folder)]}


@router.post("/api/orgs/{org_id}/backups", status_code=201)
def create_backup(body: BackupIn, ctx: OrgContext = Depends(org_access(Role.OWNER)),
                  db: Session = Depends(get_db)) -> dict:
    info = backup.create(db, custom=body.folder)
    audit.record(db, "backup.created", actor=ctx.user.id, org_id=ctx.org.id, target_type="org",
                 target_id=ctx.org.id, name=info.name, folder=str(info.path.parent), size=info.size)
    db.commit()
    return {**info.as_dict(), "folder": str(info.path.parent)}


@router.get("/api/orgs/{org_id}/backups/{name}/download")
def download_backup(name: str, folder: str | None = Query(None, max_length=400),
                    ctx: OrgContext = Depends(org_access(Role.OWNER))) -> FileResponse:
    path = backup.find(name, folder)
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


@router.put("/api/orgs/{org_id}/backups/upload", status_code=201)
async def upload_backup(org_id: uuid.UUID, request: Request, name: str = Query(..., max_length=200)) -> dict:
    """Upload a .cvbackup file (raw body) into the backup folder. Restore it afterwards."""
    await run_in_threadpool(_owner_check, org_id, request)
    target = backup.upload_target(name)
    part = target.with_name(target.name + ".part")
    size = 0
    try:
        with part.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > backup.MAX_UPLOAD_BYTES:
                    raise AppError("The file is too large.", code="too_large", status=413)
                await asyncio.to_thread(fh.write, chunk)
        info = await run_in_threadpool(backup.finish_upload, part, target)
    finally:
        part.unlink(missing_ok=True)
    return info.as_dict()


def _owner_check(org_id: uuid.UUID, request: Request) -> None:
    from ..saas.deps import session_token
    from ..saas.errors import NotAuthenticated
    from ..saas.services import orgs

    with session_factory()() as db:
        user = auth.user_from_token(db, session_token(request))
        if user is None:
            raise NotAuthenticated("Please log in.")
        orgs.access(db, org_id, user, Role.OWNER)


@router.post("/api/orgs/{org_id}/backups/{name}/restore")
def restore_backup(name: str, body: RestoreIn, response: Response,
                   ctx: OrgContext = Depends(org_access(Role.OWNER)), db: Session = Depends(get_db)) -> dict:
    """Replace ALL data with this backup (a safety backup of the current state is made first)."""
    if body.confirm.strip() != ctx.org.name:
        raise AppError("Type the business name exactly to confirm.", code="confirm",
                       fields={"confirm": "Does not match"})
    path: Path = backup.find(name, body.folder)
    email = ctx.user.email
    result = backup.restore(db, path, actor_email=email)
    clear_session_cookie(response)  # the restored database has other login sessions
    from ..db import db_session
    from ..saas.models import Organization

    with db_session() as fresh:
        org = fresh.query(Organization).order_by(Organization.created_at).first()
        if org is not None:
            audit.record(fresh, "backup.restored", actor=None, org_id=org.id, target_type="org",
                         target_id=org.id,
                         name=path.name, by=email, safety_backup=result["safety_backup"])
    return {**result, "message": "Restored. Please log in again."}
