"""Accounts and login: sign up, log in/out, sessions, email confirmation, password reset."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session

from ...db import utcnow
from ...settings import get_settings
from .. import security
from ..errors import AppError, Conflict, Forbidden, NotAuthenticated
from ..models import TokenPurpose, User
from ..repositories import orgs as org_repo
from ..repositories import users as repo
from . import audit, email, ratelimit

log = logging.getLogger(__name__)

# Sliding sessions: refresh "last seen" (and push the expiry) at most this often.
_TOUCH_EVERY = timedelta(minutes=10)


@dataclass
class LoginResult:
    user: User
    token: str  # raw cookie value (only the hash is stored)


def check_password(password: str, email_addr: str) -> None:
    problem = security.password_problem(password, email_addr)
    if problem:
        raise AppError(problem, code="weak_password", status=422, fields={"password": problem})


# --- sign up / log in -----------------------------------------------------------------------

def signup(db: Session, *, email_addr: str, name: str, password: str, verified: bool = False) -> User:
    """Create a password account. The caller creates the organization or accepts an invite."""
    email_addr = repo.normalize_email(email_addr)
    check_password(password, email_addr)
    if repo.get_user_by_email(db, email_addr):
        raise Conflict("An account with this email already exists. Please log in.", code="email_taken",
                       fields={"email": "Already registered"})
    user = repo.add_user(
        db, email=email_addr, name=name, password_hash=security.hash_password(password),
        verified_at=utcnow() if verified else None,
    )
    audit.record(db, "user.signup", actor=user.id, target_type="user", target_id=user.id, method="password")
    if not verified:
        send_verification(db, user)
    return user


def login(db: Session, *, email_addr: str, password: str, ip: str) -> User:
    """Check email + password with brute-force limits per email and per IP."""
    max_n, window = ratelimit.login_limits()
    if ratelimit.count("login-email", email_addr) >= max_n or ratelimit.count("login-ip", ip) >= max_n * 5:
        raise AppError("Too many failed logins. Please wait 15 minutes or reset your password.",
                       code="too_many_requests", status=429)
    user = repo.get_user_by_email(db, email_addr)
    if not security.verify_password(password, user.password_hash if user else None) or user is None:
        ratelimit.hit("login-email", email_addr, window)
        ratelimit.hit("login-ip", ip, window)
        raise AppError("Wrong email or password.", code="invalid_credentials", status=401)
    if not user.is_active:
        raise Forbidden("This account is disabled.", code="account_disabled")
    ratelimit.reset("login-email", email_addr)
    if user.password_hash and security.needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(password)
    return user


def start_session(db: Session, user: User) -> str:
    """Create a login session and return the raw cookie token."""
    token = security.new_token()
    expires = utcnow() + timedelta(days=get_settings().session_days)
    repo.add_session(db, user.id, security.token_hash(token), expires)
    user.last_login_at = utcnow()
    return token


def user_from_token(db: Session, token: str | None) -> User | None:
    """The logged-in user for a cookie value, or None (unknown, expired, disabled)."""
    if not token or len(token) > 200:
        return None
    row = repo.get_session_by_hash(db, security.token_hash(token))
    now = utcnow()
    if row is None or row.expires_at <= now:
        return None
    user = row.user
    if not user.is_active:
        return None
    if now - row.last_seen_at > _TOUCH_EVERY:
        row.last_seen_at = now
        row.expires_at = now + timedelta(days=get_settings().session_days)
        db.commit()
    return user


def logout(db: Session, token: str | None) -> None:
    if token:
        repo.delete_session(db, security.token_hash(token))


def logout_everywhere(db: Session, user: User, keep_token: str | None) -> int:
    keep = security.token_hash(keep_token) if keep_token else None
    return repo.delete_user_sessions(db, user.id, keep_hash=keep)


# --- email confirmation ---------------------------------------------------------------------

def _issue_token(db: Session, user: User, purpose: TokenPurpose, lifetime: timedelta) -> str:
    now = utcnow()
    repo.use_open_tokens(db, user.id, purpose.value, now)  # only the newest link works
    token = security.new_token()
    repo.add_email_token(db, user.id, purpose.value, security.token_hash(token), now + lifetime)
    return token


def send_verification(db: Session, user: User) -> None:
    settings = get_settings()
    if user.email_verified:
        return
    token = _issue_token(db, user, TokenPurpose.VERIFY_EMAIL, timedelta(hours=settings.verify_email_hours))
    url = f"{settings.web_url}/verify-email?token={token}"
    email.queue(db, email.verify_email_mail(user.email, user.name, url, settings.verify_email_hours))


def _use_token(db: Session, token: str, purpose: TokenPurpose) -> User:
    row = repo.get_email_token(db, security.token_hash(token), purpose.value)
    now = utcnow()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise AppError("This link is invalid or has expired. Please ask for a new one.",
                       code="invalid_token", status=400)
    row.used_at = now
    user = repo.get_user(db, row.user_id)
    assert user is not None
    return user


def verify_email(db: Session, token: str) -> User:
    user = _use_token(db, token, TokenPurpose.VERIFY_EMAIL)
    if not user.email_verified:
        user.email_verified_at = utcnow()
        audit.record(db, "user.email_verified", actor=user.id, target_type="user", target_id=user.id)
    return user


# --- passwords ------------------------------------------------------------------------------

def forgot_password(db: Session, email_addr: str, ip: str) -> None:
    """Send a reset link if the account exists. The answer is the same either way."""
    ratelimit.limit("reset-ip", ip, 20, 3600)
    if ratelimit.hit("reset-email", email_addr, 3600) > 3:
        return  # quietly drop: max 3 reset mails per hour per address
    user = repo.get_user_by_email(db, email_addr)
    if user is None or not user.is_active:
        return
    settings = get_settings()
    token = _issue_token(db, user, TokenPurpose.RESET_PASSWORD,
                         timedelta(minutes=settings.reset_password_minutes))
    url = f"{settings.web_url}/reset-password?token={token}"
    email.queue(db, email.reset_password_mail(user.email, user.name, url, settings.reset_password_minutes))


def reset_password(db: Session, token: str, new_password: str) -> User:
    """Set a new password from an email link. Logs out all sessions."""
    row = repo.get_email_token(db, security.token_hash(token), TokenPurpose.RESET_PASSWORD.value)
    if row is not None:
        user = repo.get_user(db, row.user_id)
        if user is not None:
            check_password(new_password, user.email)  # check before the one-time token is used
    user = _use_token(db, token, TokenPurpose.RESET_PASSWORD)
    user.password_hash = security.hash_password(new_password)
    if not user.email_verified:
        user.email_verified_at = utcnow()  # the link proved the address
    repo.delete_user_sessions(db, user.id)
    ratelimit.reset("login-email", user.email)
    audit.record(db, "user.password_reset", actor=user.id, target_type="user", target_id=user.id)
    email.queue(db, email.password_changed_mail(user.email, user.name))
    return user


def change_password(db: Session, user: User, current: str | None, new_password: str,
                    keep_token: str | None) -> None:
    """Change (or, for Google-only accounts, set) the password. Other sessions are logged out."""
    if user.password_hash and (not current or not security.verify_password(current, user.password_hash)):
        raise AppError("Your current password is wrong.", code="invalid_credentials", status=400,
                       fields={"current_password": "Wrong password"})
    check_password(new_password, user.email)
    user.password_hash = security.hash_password(new_password)
    logout_everywhere(db, user, keep_token)
    audit.record(db, "user.password_changed", actor=user.id, target_type="user", target_id=user.id)
    email.queue(db, email.password_changed_mail(user.email, user.name))


# --- account --------------------------------------------------------------------------------

def update_profile(db: Session, user: User, name: str) -> User:
    user.name = name.strip()
    return user


def delete_account(db: Session, user: User, password: str | None) -> None:
    """Delete the account (GDPR right to erasure).

    Organizations where the user is the only member are deleted too. If the user is the
    last owner of an organization that has other members, they must hand over ownership first.
    """
    if user.password_hash and (not password or not security.verify_password(password, user.password_hash)):
        raise AppError("Please enter your password to delete the account.", code="invalid_credentials",
                       status=400, fields={"password": "Wrong password"})
    for org, role in org_repo.orgs_of_user(db, user.id):
        members = org_repo.count_members(db, org.id)
        if members == 1:
            audit.record(db, "org.deleted", actor=None, target_type="org", target_id=org.id,
                         name=org.name, reason="last member deleted account")
            org_repo.delete_org(db, org)
        elif role == "owner" and org_repo.count_owners(db, org.id) == 1:
            raise Conflict(
                f"You are the only owner of \"{org.name}\". Make another member owner first.",
                code="last_owner",
            )
    db.delete(user)
    db.flush()


def require_user(user: User | None) -> User:
    if user is None:
        raise NotAuthenticated("Please log in.")
    return user


def get_user(db: Session, user_id: uuid.UUID) -> User | None:
    return repo.get_user(db, user_id)
