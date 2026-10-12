"""API keys for the public, read-only API (/api/v1).

A key looks like ``cvk_`` + 43 random characters (256 bit). Only its SHA-256 hash is stored, the key
is shown once. Admins create and revoke keys. Every key is limited to CV_API_RATE_PER_MINUTE requests
per minute.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.errors import AppError, NotAuthenticated, NotFound
from ...saas.models import Organization, User
from ...saas.services import audit, ratelimit
from ...settings import get_settings
from ..models import ApiKey

PREFIX = "cvk_"
MAX_KEYS = 20


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(db: Session, org: Organization, actor: User, name: str) -> tuple[ApiKey, str]:
    active = db.scalar(
        select(func.count(ApiKey.id)).where(ApiKey.org_id == org.id, ApiKey.revoked_at.is_(None))
    )
    if (active or 0) >= MAX_KEYS:
        raise AppError(f"At most {MAX_KEYS} active keys.", code="limit", status=409)
    key = PREFIX + secrets.token_urlsafe(32)
    row = ApiKey(org_id=org.id, name=name, prefix=key[:10], key_hash=_hash(key), created_by=actor.id)
    db.add(row)
    db.flush()
    audit.record(
        db,
        "api_key.created",
        actor=actor.id,
        org_id=org.id,
        target_type="api_key",
        target_id=row.id,
        name=name,
    )
    return row, key


def list_keys(db: Session, org: Organization) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).where(ApiKey.org_id == org.id).order_by(ApiKey.created_at.desc())))


def revoke_key(db: Session, org: Organization, actor: User, key_id: uuid.UUID) -> ApiKey:
    row = db.scalar(select(ApiKey).where(ApiKey.org_id == org.id, ApiKey.id == key_id))
    if row is None:
        raise NotFound("Key not found.")
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        audit.record(
            db,
            "api_key.revoked",
            actor=actor.id,
            org_id=org.id,
            target_type="api_key",
            target_id=row.id,
            name=row.name,
        )
    return row


def org_from_key(db: Session, request: Request) -> tuple[Organization, ApiKey]:
    """Authenticate a public API request (``Authorization: Bearer cvk_...``) and count it."""
    header = request.headers.get("authorization", "")
    key = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not key.startswith(PREFIX):
        raise NotAuthenticated(
            "Send your API key as 'Authorization: Bearer cvk_...'.", code="api_key_missing"
        )
    row = db.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(key)))
    if row is None or row.revoked_at is not None:
        raise NotAuthenticated("This API key is not valid (wrong or revoked).", code="api_key_invalid")
    limit = get_settings().api_rate_per_minute
    ratelimit.limit(
        "api_key", str(row.id), limit, 60, f"Too many requests: at most {limit} per minute per key."
    )
    now = utcnow()
    if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
        row.last_used_at = now  # written at most once a minute
        db.commit()
    org = db.get(Organization, row.org_id)
    return org, row
