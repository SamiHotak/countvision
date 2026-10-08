"""Audit log: one call per important action. Kept small on purpose (no IPs, no user agents)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from ..repositories import orgs as repo


def record(db: Session, action: str, *, actor: uuid.UUID | None, org_id: uuid.UUID | None = None,
           target_type: str | None = None, target_id: uuid.UUID | str | None = None,
           **meta: Any) -> None:
    """Write one audit entry inside the current transaction."""
    repo.add_audit(
        db, org_id=org_id, actor_user_id=actor, action=action, target_type=target_type,
        target_id=str(target_id) if target_id is not None else None, meta=meta,
    )
