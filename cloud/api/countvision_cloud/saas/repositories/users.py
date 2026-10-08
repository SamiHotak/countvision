"""Database access for users, login sessions and email tokens. No business rules here."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..models import EmailToken, User, UserSession


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_user(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def get_user_by_google_sub(db: Session, sub: str) -> User | None:
    return db.scalar(select(User).where(User.google_sub == sub))


def add_user(db: Session, *, email: str, name: str, password_hash: str | None,
             google_sub: str | None = None, verified_at: datetime | None = None) -> User:
    user = User(
        email=normalize_email(email),
        name=name.strip(),
        password_hash=password_hash,
        google_sub=google_sub,
        email_verified_at=verified_at,
    )
    db.add(user)
    db.flush()
    return user


# --- login sessions -------------------------------------------------------------------------

def add_session(db: Session, user_id: uuid.UUID, token_hash: str, expires_at: datetime) -> UserSession:
    row = UserSession(user_id=user_id, token_hash=token_hash, expires_at=expires_at)
    db.add(row)
    db.flush()
    return row


def get_session_by_hash(db: Session, token_hash: str) -> UserSession | None:
    return db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))


def delete_session(db: Session, token_hash: str) -> None:
    db.execute(delete(UserSession).where(UserSession.token_hash == token_hash))


def delete_user_sessions(db: Session, user_id: uuid.UUID, keep_hash: str | None = None) -> int:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_hash:
        stmt = stmt.where(UserSession.token_hash != keep_hash)
    return db.execute(stmt).rowcount or 0


def delete_expired_sessions(db: Session, now: datetime) -> int:
    return db.execute(delete(UserSession).where(UserSession.expires_at <= now)).rowcount or 0


# --- email tokens ---------------------------------------------------------------------------

def add_email_token(db: Session, user_id: uuid.UUID, purpose: str, token_hash: str,
                    expires_at: datetime) -> EmailToken:
    row = EmailToken(user_id=user_id, purpose=purpose, token_hash=token_hash, expires_at=expires_at)
    db.add(row)
    db.flush()
    return row


def get_email_token(db: Session, token_hash: str, purpose: str) -> EmailToken | None:
    return db.scalar(
        select(EmailToken).where(EmailToken.token_hash == token_hash, EmailToken.purpose == purpose)
    )


def use_open_tokens(db: Session, user_id: uuid.UUID, purpose: str, now: datetime) -> None:
    """Mark all open tokens of this purpose as used (an older reset link stops working)."""
    db.execute(
        update(EmailToken)
        .where(EmailToken.user_id == user_id, EmailToken.purpose == purpose, EmailToken.used_at.is_(None))
        .values(used_at=now)
    )


def delete_old_tokens(db: Session, before: datetime) -> int:
    return db.execute(delete(EmailToken).where(EmailToken.expires_at <= before)).rowcount or 0
