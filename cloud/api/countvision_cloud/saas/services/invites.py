"""Invitations by email.

- Admins and owners invite; only owners can invite owners.
- The link is only sent by email (never shown to the inviter), so the link proves that
  the person owns the address: accepting it also confirms the email.
- Only a user with the SAME email address can accept.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from ...db import utcnow
from ...settings import get_settings
from .. import security
from ..errors import AppError, Conflict, Forbidden, NotFound
from ..models import Invite, Membership, Organization, Role, User
from ..repositories import orgs as repo
from ..repositories import users as user_repo
from . import audit, auth, email, ratelimit

MAX_OPEN_INVITES = 50


def create(db: Session, org: Organization, actor: User, actor_membership: Membership,
           email_addr: str, role: Role) -> Invite:
    """Create (or renew) an invite and email the link."""
    actor_role = Role(actor_membership.role)
    if not actor_role.at_least(Role.ADMIN):
        raise Forbidden("Only admins and owners can invite people.")
    if role == Role.OWNER and actor_role != Role.OWNER:
        raise Forbidden("Only owners can invite new owners.")
    ratelimit.limit("invite-user", str(actor.id), 30, 3600,
                    "You sent many invitations in the last hour. Please wait a bit.")
    email_addr = user_repo.normalize_email(email_addr)
    existing_user = user_repo.get_user_by_email(db, email_addr)
    if existing_user and repo.get_membership(db, org.id, existing_user.id):
        raise Conflict("This person is already a member.", code="already_member",
                       fields={"email": "Already a member"})
    now = utcnow()
    old = repo.get_open_invite_for_email(db, org.id, email_addr)
    if old is not None:
        old.revoked_at = now  # renewing = new link, the old link stops working
        db.flush()
    elif len(repo.list_open_invites(db, org.id)) >= MAX_OPEN_INVITES:
        raise AppError(f"At most {MAX_OPEN_INVITES} open invitations. Revoke some first.", code="limit")

    settings = get_settings()
    token = security.new_token()
    invite = repo.add_invite(
        db, org_id=org.id, email=email_addr, role=role, token_hash=security.token_hash(token),
        invited_by=actor.id, expires_at=now + timedelta(days=settings.invite_days),
    )
    url = f"{settings.web_url}/invite/{token}"
    email.queue(db, email.invite_mail(email_addr, actor.name, org.name, role.value, url,
                                      settings.invite_days))
    audit.record(db, "member.invited", actor=actor.id, org_id=org.id, target_type="invite",
                 target_id=invite.id, email=email_addr, role=role.value, renewed=old is not None)
    return invite


def revoke(db: Session, org: Organization, actor: User, actor_membership: Membership,
           invite_id: uuid.UUID) -> None:
    if not Role(actor_membership.role).at_least(Role.ADMIN):
        raise Forbidden("Only admins and owners can revoke invitations.")
    invite = repo.get_invite(db, org.id, invite_id)
    if invite is None or not invite.is_open:
        raise NotFound("Invitation not found.")
    if invite.role == Role.OWNER.value and actor_membership.role != Role.OWNER.value:
        raise Forbidden("Only owners can revoke an owner invitation.")
    invite.revoked_at = utcnow()
    audit.record(db, "invite.revoked", actor=actor.id, org_id=org.id, target_type="invite",
                 target_id=invite.id, email=invite.email)


def list_open(db: Session, org: Organization) -> list[Invite]:
    return repo.list_open_invites(db, org.id)


@dataclass
class InvitePreview:
    invite: Invite
    status: str
    account_exists: bool


def _by_token(db: Session, token: str) -> Invite:
    invite = repo.get_invite_by_hash(db, security.token_hash(token)) if len(token) < 200 else None
    if invite is None:
        raise NotFound("This invitation does not exist.", code="invite_not_found")
    return invite


def preview(db: Session, token: str) -> InvitePreview:
    """Public view of an invite for the invite page (only the token holder sees it)."""
    invite = _by_token(db, token)
    exists = user_repo.get_user_by_email(db, invite.email) is not None
    return InvitePreview(invite=invite, status=invite.status(utcnow()), account_exists=exists)


def _check_usable(invite: Invite) -> None:
    status = invite.status(utcnow())
    if status != "pending":
        messages = {
            "accepted": "This invitation was already used.",
            "revoked": "This invitation was cancelled. Ask for a new one.",
            "expired": "This invitation has expired. Ask for a new one.",
        }
        raise AppError(messages[status], code=f"invite_{status}", status=410)


def accept(db: Session, token: str, user: User) -> Organization:
    """The logged-in user accepts. The email must match."""
    invite = _by_token(db, token)
    _check_usable(invite)
    if invite.email != user.email:
        raise Forbidden(
            f"This invitation is for {invite.email}, but you are logged in as {user.email}. "
            "Log out and log in (or sign up) with the invited address.",
            code="invite_wrong_user",
        )
    return _join(db, invite, user)


def signup_and_accept(db: Session, token: str, *, email_addr: str, name: str, password: str
                      ) -> tuple[User, Organization]:
    """Create an account from the invite page (email confirmed by the link) and join."""
    invite = _by_token(db, token)
    _check_usable(invite)
    if user_repo.normalize_email(email_addr) != invite.email:
        raise AppError(f"Please sign up with the invited address {invite.email}.", code="invite_wrong_user",
                       fields={"email": "Must be the invited address"})
    user = auth.signup(db, email_addr=email_addr, name=name, password=password, verified=True)
    return user, _join(db, invite, user)


def _join(db: Session, invite: Invite, user: User) -> Organization:
    org = invite.organization
    now = utcnow()
    if repo.get_membership(db, org.id, user.id) is None:
        repo.add_membership(db, org.id, user.id, Role(invite.role))
    invite.accepted_at = now
    invite.accepted_by = user.id
    if not user.email_verified:
        user.email_verified_at = now  # the emailed link proved the address
    audit.record(db, "invite.accepted", actor=user.id, org_id=org.id, target_type="invite",
                 target_id=invite.id, email=user.email, role=invite.role)
    return org
