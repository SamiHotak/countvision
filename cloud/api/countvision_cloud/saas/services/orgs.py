"""Organizations and members, with the role rules.

Role rules (owner > admin > member > viewer):
- admins and owners manage members and invites; nobody can act on a role above their own;
- only owners can make someone an owner, change an owner or remove an owner;
- the last owner cannot leave, be removed or be demoted (an organization always has an owner);
- non-members get 404, so organization ids cannot be probed.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid

from sqlalchemy.orm import Session

from ..errors import AppError, Conflict, Forbidden, NotFound
from ..models import Membership, Organization, Role, User
from ..repositories import orgs as repo
from . import audit

MAX_ORGS_PER_USER = 20


def slugify(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60] or "org"


def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    while repo.slug_taken(db, slug):
        slug = f"{base}-{secrets.token_hex(3)}"
    return slug


def create_org(db: Session, user: User, name: str) -> Organization:
    if len(repo.orgs_of_user(db, user.id)) >= MAX_ORGS_PER_USER:
        raise AppError(f"You can be in at most {MAX_ORGS_PER_USER} organizations.", code="limit")
    org = repo.add_org(db, name=name, slug=unique_slug(db, name), created_by=user.id)
    repo.add_membership(db, org.id, user.id, Role.OWNER)
    audit.record(db, "org.created", actor=user.id, org_id=org.id, target_type="org", target_id=org.id,
                 name=org.name)
    return org


def access(db: Session, org_id: uuid.UUID, user: User, min_role: Role = Role.VIEWER
           ) -> tuple[Organization, Membership]:
    """The organization and the user's membership, or 404 / 403."""
    membership = repo.get_membership(db, org_id, user.id)
    org = repo.get_org(db, org_id) if membership else None
    if membership is None or org is None:
        raise NotFound("Organization not found.")
    if not Role(membership.role).at_least(min_role):
        raise Forbidden(f"You need the role {min_role.value} or higher for this.")
    return org, membership


def rename(db: Session, org: Organization, actor: User, name: str) -> Organization:
    old = org.name
    org.name = name.strip()
    audit.record(db, "org.renamed", actor=actor.id, org_id=org.id, target_type="org", target_id=org.id,
                 old=old, new=org.name)
    return org


def delete(db: Session, org: Organization, actor: User, confirm_name: str) -> None:
    if confirm_name.strip() != org.name:
        raise AppError("Type the exact organization name to delete it.", code="confirm_mismatch",
                       fields={"confirm_name": "Does not match"})
    audit.record(db, "org.deleted", actor=actor.id, org_id=None, target_type="org", target_id=org.id,
                 name=org.name)
    repo.delete_org(db, org)


def _guard_owner_change(db: Session, org_id: uuid.UUID, target: Membership, actor_role: Role,
                        new_role: Role | None) -> None:
    """Checks shared by 'change role' and 'remove member'."""
    target_role = Role(target.role)
    if target_role == Role.OWNER or new_role == Role.OWNER:
        if actor_role != Role.OWNER:
            raise Forbidden("Only owners can add, change or remove owners.")
        if target_role == Role.OWNER and new_role != Role.OWNER and repo.count_owners(db, org_id) <= 1:
            raise Conflict("An organization needs at least one owner. Make someone else owner first.",
                           code="last_owner")
    elif not actor_role.at_least(target_role):
        raise Forbidden("You cannot change a member with a higher role than yours.")


def change_role(db: Session, org: Organization, actor: User, actor_membership: Membership,
                membership_id: uuid.UUID, new_role: Role) -> Membership:
    target = repo.get_membership_by_id(db, org.id, membership_id)
    if target is None:
        raise NotFound("Member not found.")
    actor_role = Role(actor_membership.role)
    if not actor_role.at_least(Role.ADMIN):
        raise Forbidden("Only admins and owners can change roles.")
    if target.role == new_role.value:
        return target
    _guard_owner_change(db, org.id, target, actor_role, new_role)
    old = target.role
    target.role = new_role.value
    audit.record(db, "member.role_changed", actor=actor.id, org_id=org.id, target_type="user",
                 target_id=target.user_id, email=target.user.email, old=old, new=new_role.value)
    return target


def remove_member(db: Session, org: Organization, actor: User, actor_membership: Membership,
                  membership_id: uuid.UUID) -> None:
    target = repo.get_membership_by_id(db, org.id, membership_id)
    if target is None:
        raise NotFound("Member not found.")
    if target.user_id == actor.id:
        leave(db, org, actor, actor_membership)
        return
    actor_role = Role(actor_membership.role)
    if not actor_role.at_least(Role.ADMIN):
        raise Forbidden("Only admins and owners can remove members.")
    _guard_owner_change(db, org.id, target, actor_role, None)
    if Role(target.role) == Role.OWNER and repo.count_owners(db, org.id) <= 1:
        raise Conflict("An organization needs at least one owner.", code="last_owner")
    audit.record(db, "member.removed", actor=actor.id, org_id=org.id, target_type="user",
                 target_id=target.user_id, email=target.user.email, role=target.role)
    repo.delete_membership(db, target)


def leave(db: Session, org: Organization, actor: User, membership: Membership) -> None:
    if Role(membership.role) == Role.OWNER and repo.count_owners(db, org.id) <= 1:
        if repo.count_members(db, org.id) > 1:
            raise Conflict("You are the only owner. Make another member owner first, or delete "
                           "the organization.", code="last_owner")
        raise Conflict("You are the only member. Delete the organization instead.", code="last_owner")
    audit.record(db, "member.left", actor=actor.id, org_id=org.id, target_type="user", target_id=actor.id,
                 email=actor.email)
    repo.delete_membership(db, membership)


def my_orgs(db: Session, user: User) -> list[tuple[Organization, str]]:
    return repo.orgs_of_user(db, user.id)


def members(db: Session, org: Organization) -> list[Membership]:
    return repo.list_members(db, org.id)
