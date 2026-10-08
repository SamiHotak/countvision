"""FastAPI dependencies: database session, current user, organization access."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..settings import get_settings
from .models import Membership, Organization, Role, User
from .services import auth, orgs


def session_token(request: Request) -> str | None:
    return request.cookies.get(get_settings().session_cookie)


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    return auth.user_from_token(db, session_token(request))


def current_user(user: User | None = Depends(optional_user)) -> User:
    return auth.require_user(user)


@dataclass
class OrgContext:
    org: Organization
    membership: Membership
    user: User

    @property
    def role(self) -> Role:
        return Role(self.membership.role)


def org_access(min_role: Role = Role.VIEWER) -> Callable[..., OrgContext]:
    """Dependency factory: the user must be a member of {org_id} with at least min_role."""

    def dependency(org_id: uuid.UUID, user: User = Depends(current_user),
                   db: Session = Depends(get_db)) -> OrgContext:
        org, membership = orgs.access(db, org_id, user, min_role)
        return OrgContext(org=org, membership=membership, user=user)

    return dependency
