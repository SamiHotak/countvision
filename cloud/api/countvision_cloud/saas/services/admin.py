"""Operator view of the whole service (the admin page): organizations, plans, usage, audit log.

Only platform admins (users.is_platform_admin, set with ``countvision-cloud make-admin``) get here.
The product adds its own facts per organization (devices, last data ...) with ``register_org_facts``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from ...db import utcnow
from ..errors import NotFound
from ..models import AuditLog, Membership, Organization, OrgBilling, Role, User
from ..plans import get_plan
from . import billing

_facts_fns: list[Callable[[Session, list[uuid.UUID]], dict[uuid.UUID, dict[str, Any]]]] = []


def register_org_facts(fn: Callable[[Session, list[uuid.UUID]], dict[uuid.UUID, dict[str, Any]]]) -> None:
    """fn(db, org_ids) -> {org_id: {"devices": 2, ...}} shown in the admin list."""
    if fn not in _facts_fns:
        _facts_fns.append(fn)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _owners(db: Session, org_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    rows = db.execute(
        select(Membership.org_id, User.email)
        .join(User, User.id == Membership.user_id)
        .where(Membership.org_id.in_(org_ids), Membership.role == Role.OWNER.value)
        .order_by(Membership.created_at)
    )
    out: dict[uuid.UUID, str] = {}
    for org_id, email in rows:
        out.setdefault(org_id, email)
    return out


def _org_rows(db: Session, orgs: list[Organization]) -> list[dict[str, Any]]:
    ids = [o.id for o in orgs]
    if not ids:
        return []
    rows = {r.org_id: r for r in db.scalars(select(OrgBilling).where(OrgBilling.org_id.in_(ids)))}
    members = dict(
        db.execute(
            select(Membership.org_id, func.count())
            .where(Membership.org_id.in_(ids))
            .group_by(Membership.org_id)
        ).all()
    )
    owners = _owners(db, ids)
    facts: dict[uuid.UUID, dict[str, Any]] = {}
    for fn in _facts_fns:
        for org_id, values in fn(db, ids).items():
            facts.setdefault(org_id, {}).update(values)
    out = []
    now = utcnow()
    for org in orgs:
        row = rows.get(org.id)
        ent = billing.entitlement_of(row, now)
        out.append(
            {
                "id": str(org.id),
                "name": org.name,
                "slug": org.slug,
                "created_at": _iso(org.created_at),
                "owner_email": owners.get(org.id),
                "members": int(members.get(org.id, 0)),
                "state": ent.state,
                "active": ent.active,
                "plan": ent.plan.id,
                "plan_name": ent.plan.name,
                "limits": ent.plan.limits,
                "usage": billing.usage(db, org.id),
                "trial_ends_at": _iso(row.trial_ends_at) if row else None,
                "period_end": _iso(row.current_period_end) if row else None,
                "cancel_at_period_end": bool(row and row.cancel_at_period_end),
                "comp_plan": row.comp_plan if row else None,
                "comp_until": _iso(row.comp_until) if row else None,
                "comp_note": row.comp_note if row else None,
                "stripe_customer_id": row.stripe_customer_id if row else None,
                "sub_status": row.sub_status if row else None,
                **facts.get(org.id, {}),
            }
        )
    return out


def list_orgs(db: Session, q: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    stmt = select(Organization).order_by(Organization.created_at.desc()).limit(limit)
    if q:
        like = f"%{q.strip().lower()}%"
        owner_match = (
            select(Membership.org_id)
            .join(User, User.id == Membership.user_id)
            .where(func.lower(User.email).like(like))
        )
        stmt = stmt.where(
            or_(
                func.lower(Organization.name).like(like),
                Organization.slug.like(like),
                Organization.id.in_(owner_match),
            )
        )
    return _org_rows(db, list(db.scalars(stmt)))


def get_org(db: Session, org_id: uuid.UUID) -> Organization:
    org = db.get(Organization, org_id)
    if org is None:
        raise NotFound("Organization not found.")
    return org


def org_detail(db: Session, org: Organization) -> dict[str, Any]:
    out = _org_rows(db, [org])[0]
    out["audit"] = audit_rows(db, org_id=org.id, limit=30)
    out["members_list"] = [
        {"email": m.user.email, "name": m.user.name, "role": m.role}
        for m in db.scalars(
            select(Membership)
            .options(joinedload(Membership.user))
            .where(Membership.org_id == org.id)
            .order_by(Membership.created_at)
        )
    ]
    return out


def overview(db: Session) -> dict[str, Any]:
    now = utcnow()
    rows = {r.org_id: r for r in db.scalars(select(OrgBilling))}
    org_ids = list(db.scalars(select(Organization.id)))
    states: dict[str, int] = {}
    mrr = 0
    paying = 0
    for org_id in org_ids:
        row = rows.get(org_id)
        ent = billing.entitlement_of(row, now)
        states[ent.state] = states.get(ent.state, 0) + 1
        plan = get_plan(row.plan) if row else None
        if row and plan and row.sub_status in ("active", "past_due") and plan.price_eur:
            paying += 1
            mrr += plan.price_eur
    week = now - timedelta(days=7)
    return {
        "organizations": len(org_ids),
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
        "new_orgs_7d": db.scalar(
            select(func.count()).select_from(Organization).where(Organization.created_at >= week)
        )
        or 0,
        "states": states,
        "paying": paying,
        "mrr_eur": mrr,  # monthly revenue from paid plans, before VAT
        "trial_ending_3d": sum(
            1
            for org_id in org_ids
            if (r := rows.get(org_id)) is not None
            and billing.entitlement_of(r, now).state == "trial"
            and r.trial_ends_at - now <= timedelta(days=3)
        ),
    }


def audit_rows(
    db: Session,
    *,
    org_id: uuid.UUID | None = None,
    action: str | None = None,
    limit: int = 100,
    before_id: int | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(AuditLog, Organization.name)
        .outerjoin(Organization, Organization.id == AuditLog.org_id)
        .options(joinedload(AuditLog.actor))
        .order_by(AuditLog.id.desc())
        .limit(min(limit, 500))
    )
    if org_id is not None:
        stmt = stmt.where(AuditLog.org_id == org_id)
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action.strip()}%"))
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    return [
        {
            "id": a.id,
            "action": a.action,
            "org_id": str(a.org_id) if a.org_id else None,
            "org_name": name,
            "actor": a.actor.email if a.actor else None,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "meta": a.meta,
            "created_at": _iso(a.created_at),
        }
        for a, name in db.execute(stmt).unique()
    ]
