"""Database access for organizations, memberships, invites and the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload

from ..models import AuditLog, Invite, Membership, Organization, Role, User


def get_org(db: Session, org_id: uuid.UUID) -> Organization | None:
    return db.get(Organization, org_id)


def slug_taken(db: Session, slug: str) -> bool:
    return db.scalar(select(func.count()).select_from(Organization).where(Organization.slug == slug)) > 0


def add_org(db: Session, *, name: str, slug: str, created_by: uuid.UUID | None) -> Organization:
    org = Organization(name=name.strip(), slug=slug, created_by=created_by)
    db.add(org)
    db.flush()
    return org


def delete_org(db: Session, org: Organization) -> None:
    db.delete(org)
    db.flush()


# --- memberships ----------------------------------------------------------------------------

def get_membership(db: Session, org_id: uuid.UUID, user_id: uuid.UUID) -> Membership | None:
    return db.scalar(select(Membership).where(Membership.org_id == org_id, Membership.user_id == user_id))


def get_membership_by_id(db: Session, org_id: uuid.UUID, membership_id: uuid.UUID) -> Membership | None:
    return db.scalar(
        select(Membership)
        .options(joinedload(Membership.user))
        .where(Membership.org_id == org_id, Membership.id == membership_id)
    )


def add_membership(db: Session, org_id: uuid.UUID, user_id: uuid.UUID, role: Role) -> Membership:
    row = Membership(org_id=org_id, user_id=user_id, role=role.value)
    db.add(row)
    db.flush()
    return row


def list_members(db: Session, org_id: uuid.UUID) -> list[Membership]:
    return list(
        db.scalars(
            select(Membership)
            .options(joinedload(Membership.user))
            .where(Membership.org_id == org_id)
            .join(User, User.id == Membership.user_id)
            .order_by(Membership.created_at, User.email)
        )
    )


def count_owners(db: Session, org_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count()).select_from(Membership)
        .where(Membership.org_id == org_id, Membership.role == Role.OWNER.value)
    ) or 0


def count_members(db: Session, org_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).select_from(Membership).where(Membership.org_id == org_id)) or 0


def orgs_of_user(db: Session, user_id: uuid.UUID) -> list[tuple[Organization, str]]:
    rows = db.execute(
        select(Organization, Membership.role)
        .join(Membership, Membership.org_id == Organization.id)
        .where(Membership.user_id == user_id)
        .order_by(Organization.name)
    )
    return [(org, role) for org, role in rows]


def delete_membership(db: Session, membership: Membership) -> None:
    db.delete(membership)
    db.flush()


# --- invites --------------------------------------------------------------------------------

def add_invite(db: Session, *, org_id: uuid.UUID, email: str, role: Role, token_hash: str,
               invited_by: uuid.UUID, expires_at: datetime) -> Invite:
    row = Invite(org_id=org_id, email=email, role=role.value, token_hash=token_hash,
                 invited_by=invited_by, expires_at=expires_at)
    db.add(row)
    db.flush()
    return row


def get_open_invite_for_email(db: Session, org_id: uuid.UUID, email: str) -> Invite | None:
    return db.scalar(
        select(Invite).where(
            Invite.org_id == org_id, Invite.email == email,
            Invite.accepted_at.is_(None), Invite.revoked_at.is_(None),
        )
    )


def get_invite(db: Session, org_id: uuid.UUID, invite_id: uuid.UUID) -> Invite | None:
    return db.scalar(select(Invite).where(Invite.org_id == org_id, Invite.id == invite_id))


def get_invite_by_hash(db: Session, token_hash: str) -> Invite | None:
    return db.scalar(
        select(Invite).options(joinedload(Invite.organization), joinedload(Invite.inviter))
        .where(Invite.token_hash == token_hash)
    )


def list_open_invites(db: Session, org_id: uuid.UUID) -> list[Invite]:
    return list(
        db.scalars(
            select(Invite)
            .where(Invite.org_id == org_id, Invite.accepted_at.is_(None), Invite.revoked_at.is_(None))
            .order_by(Invite.created_at.desc())
        )
    )


def delete_dead_invites(db: Session, before: datetime) -> int:
    """Remove invites that expired (or were revoked) before the given time."""
    stmt = delete(Invite).where(
        Invite.accepted_at.is_(None),
        (Invite.expires_at <= before) | (Invite.revoked_at <= before),
    )
    return db.execute(stmt).rowcount or 0


# --- audit log ------------------------------------------------------------------------------

def add_audit(db: Session, *, org_id: uuid.UUID | None, actor_user_id: uuid.UUID | None, action: str,
              target_type: str | None = None, target_id: str | None = None,
              meta: dict[str, Any] | None = None) -> AuditLog:
    row = AuditLog(org_id=org_id, actor_user_id=actor_user_id, action=action,
                   target_type=target_type, target_id=target_id, meta=meta or {})
    db.add(row)
    db.flush()
    return row


def list_audit(db: Session, org_id: uuid.UUID, *, limit: int, before_id: int | None) -> list[AuditLog]:
    stmt = (
        select(AuditLog).options(joinedload(AuditLog.actor))
        .where(AuditLog.org_id == org_id).order_by(AuditLog.id.desc()).limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    return list(db.scalars(stmt))
