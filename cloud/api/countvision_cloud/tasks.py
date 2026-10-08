"""Background tasks."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from .celery_app import celery
from .db import db_session, utcnow
from .redis_client import get_redis
from .saas.repositories import orgs as org_repo
from .saas.repositories import users as user_repo
from .saas.routers.health import HEARTBEAT_KEY
from .saas.services import email

log = logging.getLogger(__name__)


@celery.task(name="countvision_cloud.tasks.send_email", autoretry_for=(OSError,), retry_backoff=True,
             retry_backoff_max=600, max_retries=8)
def send_email(mail: dict[str, str]) -> None:
    """Send one email. SMTP problems are retried with growing pauses (up to ~1 hour in total)."""
    email.deliver(email.Mail(**mail))


@celery.task(name="countvision_cloud.tasks.cleanup")
def cleanup() -> dict[str, int]:
    """Hourly: delete expired logins, old email tokens and dead invites (data minimisation)."""
    now = utcnow()
    with db_session() as db:
        result = {
            "sessions": user_repo.delete_expired_sessions(db, now),
            "tokens": user_repo.delete_old_tokens(db, now - timedelta(days=1)),
            "invites": org_repo.delete_dead_invites(db, now - timedelta(days=30)),
        }
    log.info("Cleanup: %s", result)
    return result


@celery.task(name="countvision_cloud.tasks.heartbeat")
def heartbeat() -> None:
    """Beat writes a timestamp, so the health check can see that timers run."""
    get_redis().set(HEARTBEAT_KEY, str(int(time.time())), ex=600)
