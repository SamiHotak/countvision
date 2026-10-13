"""/api/admin: the operator's view of all organizations (platform admins only; others get 404)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...db import get_db
from ..deps import current_user
from ..errors import NotFound
from ..models import User
from ..schemas import GrantIn, OkOut, TrialIn
from ..services import admin, billing


def platform_admin(user: User = Depends(current_user)) -> User:
    if not user.is_platform_admin:
        raise NotFound("Not found.")  # do not reveal that the admin page exists
    return user


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(platform_admin)])


@router.get("/overview")
def overview(db: Session = Depends(get_db)) -> dict[str, Any]:
    return admin.overview(db)


@router.get("/orgs")
def list_orgs(
    q: str | None = Query(None, max_length=100), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    return admin.list_orgs(db, q)


@router.get("/orgs/{org_id}")
def org_detail(org_id: uuid.UUID, db: Session = Depends(get_db)) -> dict[str, Any]:
    return admin.org_detail(db, admin.get_org(db, org_id))


@router.post("/orgs/{org_id}/trial", response_model=OkOut)
def extend_trial(
    org_id: uuid.UUID, body: TrialIn, user: User = Depends(platform_admin), db: Session = Depends(get_db)
) -> OkOut:
    row = billing.extend_trial(db, admin.get_org(db, org_id), user, body.days)
    db.commit()
    return OkOut(message=f"Trial now ends {row.trial_ends_at:%Y-%m-%d}.")


@router.post("/orgs/{org_id}/grant", response_model=OkOut)
def grant(
    org_id: uuid.UUID, body: GrantIn, user: User = Depends(platform_admin), db: Session = Depends(get_db)
) -> OkOut:
    """A free plan until a date: for pilot customers. No Stripe involved."""
    billing.grant_plan(db, admin.get_org(db, org_id), user, body.plan, body.until, body.note)
    db.commit()
    return OkOut(message="Plan granted.")


@router.delete("/orgs/{org_id}/grant", response_model=OkOut)
def remove_grant(
    org_id: uuid.UUID, user: User = Depends(platform_admin), db: Session = Depends(get_db)
) -> OkOut:
    billing.grant_plan(db, admin.get_org(db, org_id), user, None, None, None)
    db.commit()
    return OkOut(message="Grant removed.")


@router.post("/orgs/{org_id}/sync", response_model=OkOut)
def sync(org_id: uuid.UUID, user: User = Depends(platform_admin), db: Session = Depends(get_db)) -> OkOut:
    org = admin.get_org(db, org_id)
    changed = billing.sync(db, billing.billing_row(db, org.id, lock=True), actor=user.id)
    db.commit()
    return OkOut(message="Updated from Stripe." if changed else "Already up to date.")


@router.get("/audit")
def audit(
    org_id: uuid.UUID | None = None,
    action: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=500),
    before: int | None = None,
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Everything that happened, newest first (all organizations, or one; filter by action prefix)."""
    return admin.audit_rows(db, org_id=org_id, action=action, limit=limit, before_id=before)
