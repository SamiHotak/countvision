"""/api/orgs: organizations, members, invites, audit log. /api/invites: public invite page."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ...db import get_db, utcnow
from ..deps import OrgContext, current_user, org_access
from ..models import Organization, Role, User
from ..repositories import orgs as org_repo
from ..schemas import (
    AcceptOut,
    AuditOut,
    InviteIn,
    InviteOut,
    InvitePreviewOut,
    MemberOut,
    OkOut,
    OrgDeleteIn,
    OrgIn,
    OrgOut,
    RoleIn,
)
from ..services import invites, orgs

router = APIRouter(prefix="/api/orgs", tags=["organizations"])
invite_router = APIRouter(prefix="/api/invites", tags=["invites"])


def org_out(db: Session, org: Organization, role: Role) -> OrgOut:
    return OrgOut(id=org.id, name=org.name, slug=org.slug, created_at=org.created_at, my_role=role,
                  member_count=org_repo.count_members(db, org.id))


@router.get("", response_model=list[OrgOut])
def list_orgs(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[OrgOut]:
    return [org_out(db, org, Role(role)) for org, role in orgs.my_orgs(db, user)]


@router.post("", response_model=OrgOut, status_code=201)
def create_org(body: OrgIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> OrgOut:
    org = orgs.create_org(db, user, body.name)
    db.commit()
    return org_out(db, org, Role.OWNER)


@router.get("/{org_id}", response_model=OrgOut)
def get_org(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> OrgOut:
    return org_out(db, ctx.org, ctx.role)


@router.patch("/{org_id}", response_model=OrgOut)
def rename_org(body: OrgIn, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
               db: Session = Depends(get_db)) -> OrgOut:
    orgs.rename(db, ctx.org, ctx.user, body.name)
    db.commit()
    return org_out(db, ctx.org, ctx.role)


@router.post("/{org_id}/delete", response_model=OkOut)
def delete_org(body: OrgDeleteIn, ctx: OrgContext = Depends(org_access(Role.OWNER)),
               db: Session = Depends(get_db)) -> OkOut:
    orgs.delete(db, ctx.org, ctx.user, body.confirm_name)
    db.commit()
    return OkOut(message="Organization deleted.")


@router.post("/{org_id}/leave", response_model=OkOut)
def leave_org(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> OkOut:
    orgs.leave(db, ctx.org, ctx.user, ctx.membership)
    db.commit()
    return OkOut(message="You left the organization.")


# --- members --------------------------------------------------------------------------------

@router.get("/{org_id}/members", response_model=list[MemberOut])
def list_members(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> list[MemberOut]:
    return [
        MemberOut(id=m.id, user_id=m.user_id, email=m.user.email, name=m.user.name, role=Role(m.role),
                  joined_at=m.created_at, is_me=m.user_id == ctx.user.id)
        for m in orgs.members(db, ctx.org)
    ]


@router.patch("/{org_id}/members/{membership_id}", response_model=OkOut)
def change_role(membership_id: uuid.UUID, body: RoleIn, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                db: Session = Depends(get_db)) -> OkOut:
    orgs.change_role(db, ctx.org, ctx.user, ctx.membership, membership_id, body.role)
    db.commit()
    return OkOut(message="Role changed.")


@router.delete("/{org_id}/members/{membership_id}", response_model=OkOut)
def remove_member(membership_id: uuid.UUID, ctx: OrgContext = Depends(org_access()),
                  db: Session = Depends(get_db)) -> OkOut:
    orgs.remove_member(db, ctx.org, ctx.user, ctx.membership, membership_id)
    db.commit()
    return OkOut(message="Member removed.")


# --- invites --------------------------------------------------------------------------------

def invite_out(inv) -> InviteOut:
    return InviteOut(id=inv.id, email=inv.email, role=Role(inv.role), status=inv.status(utcnow()),
                     invited_by=inv.inviter.name if inv.inviter else None, created_at=inv.created_at,
                     expires_at=inv.expires_at)


@router.get("/{org_id}/invites", response_model=list[InviteOut])
def list_invites(ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                 db: Session = Depends(get_db)) -> list[InviteOut]:
    return [invite_out(i) for i in invites.list_open(db, ctx.org)]


@router.post("/{org_id}/invites", response_model=InviteOut, status_code=201)
def create_invite(body: InviteIn, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                  db: Session = Depends(get_db)) -> InviteOut:
    """Send an invitation email. Inviting the same address again sends a new link."""
    inv = invites.create(db, ctx.org, ctx.user, ctx.membership, body.email, body.role)
    db.commit()
    db.refresh(inv)
    return invite_out(inv)


@router.delete("/{org_id}/invites/{invite_id}", response_model=OkOut)
def revoke_invite(invite_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.ADMIN)),
                  db: Session = Depends(get_db)) -> OkOut:
    invites.revoke(db, ctx.org, ctx.user, ctx.membership, invite_id)
    db.commit()
    return OkOut(message="Invitation cancelled.")


# --- audit log ------------------------------------------------------------------------------

@router.get("/{org_id}/audit", response_model=list[AuditOut])
def audit_log(ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db),
              limit: int = Query(50, ge=1, le=200), before: int | None = None) -> list[AuditOut]:
    rows = org_repo.list_audit(db, ctx.org.id, limit=limit, before_id=before)
    return [
        AuditOut(id=r.id, action=r.action, actor=(r.actor.name if r.actor else None),
                 target_type=r.target_type, target_id=r.target_id, meta=r.meta, created_at=r.created_at)
        for r in rows
    ]


# --- public invite page ---------------------------------------------------------------------

@invite_router.get("/{token}", response_model=InvitePreviewOut)
def preview_invite(token: str, db: Session = Depends(get_db)) -> InvitePreviewOut:
    p = invites.preview(db, token)
    return InvitePreviewOut(
        organization=p.invite.organization.name, email=p.invite.email, role=Role(p.invite.role),
        invited_by=p.invite.inviter.name if p.invite.inviter else None, status=p.status,
        expires_at=p.invite.expires_at, account_exists=p.account_exists,
    )


@invite_router.post("/{token}/accept", response_model=AcceptOut)
def accept_invite(token: str, user: User = Depends(current_user), db: Session = Depends(get_db)) -> AcceptOut:
    org = invites.accept(db, token, user)
    db.commit()
    return AcceptOut(org_id=org.id, org_name=org.name)
