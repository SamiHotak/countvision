"""Platform tables: users, login sessions, email tokens, organizations, members, invites, audit log.

These are product-neutral on purpose, so the same "SaaS basics" can be reused for other products.
Product tables (sites, cameras, devices ...) come in their own module.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, utcnow


class Role(enum.StrEnum):
    """Roles inside an organization, strongest first."""

    OWNER = "owner"  # everything, incl. delete the organization and manage owners
    ADMIN = "admin"  # manage members, invites and settings (not owners)
    MEMBER = "member"  # use the product (later: edit sites, cameras, lines)
    VIEWER = "viewer"  # read only (dashboards, reports)

    @property
    def rank(self) -> int:
        return {Role.OWNER: 4, Role.ADMIN: 3, Role.MEMBER: 2, Role.VIEWER: 1}[self]

    def at_least(self, other: Role) -> bool:
        return self.rank >= other.rank


ROLE_VALUES = tuple(r.value for r in Role)
_ROLE_CHECK = "role IN ('owner', 'admin', 'member', 'viewer')"


class TokenPurpose(enum.StrEnum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # always lower case
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None = Google-only account
    email_verified_at: Mapped[datetime | None]
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=text("now()")
    )

    memberships: Mapped[list[Membership]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def email_verified(self) -> bool:
        return self.email_verified_at is not None


class UserSession(Base):
    """A login (cookie). Only the SHA-256 of the cookie value is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    user: Mapped[User] = relationship()


class EmailToken(Base):
    """One-time token sent by email (verify address, reset password). Stored hashed."""

    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    expires_at: Mapped[datetime] = mapped_column(index=True)
    used_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("purpose IN ('verify_email', 'reset_password')", name="purpose"),
    )


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", passive_deletes=True
    )


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),
        CheckConstraint(_ROLE_CHECK, name="role"),
    )


class Invite(Base):
    """An invitation by email. The link carries a random token; only its hash is stored."""

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    expires_at: Mapped[datetime]
    accepted_at: Mapped[datetime | None]
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None]

    organization: Mapped[Organization] = relationship()
    inviter: Mapped[User | None] = relationship(foreign_keys=[invited_by])

    __table_args__ = (
        CheckConstraint(_ROLE_CHECK, name="role"),
        # at most one open invite per email and organization
        Index(
            "uq_invites_open_email",
            "org_id",
            "email",
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    @property
    def is_open(self) -> bool:
        return self.accepted_at is None and self.revoked_at is None

    def status(self, now: datetime) -> str:
        if self.accepted_at is not None:
            return "accepted"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        return "pending"


class AuditLog(Base):
    """Who did what, and when. No IP addresses or user agents (data minimisation)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"), index=True)

    actor: Mapped[User | None] = relationship()
