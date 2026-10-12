"""Background tasks."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta

from sqlalchemy import delete

from .celery_app import celery
from .db import db_session, utcnow
from .product.models import AlertEvent, IngestBatch, PairingCode, WebhookDelivery
from .redis_client import get_redis
from .saas.repositories import orgs as org_repo
from .saas.repositories import users as user_repo
from .saas.routers.health import HEARTBEAT_KEY
from .saas.services import email

log = logging.getLogger(__name__)


@celery.task(name="countvision_cloud.tasks.send_email", autoretry_for=(OSError,), retry_backoff=True,
             retry_backoff_max=600, max_retries=8)
def send_email(mail: dict) -> None:
    """Send one email. SMTP problems are retried with growing pauses (up to ~1 hour in total)."""
    email.deliver(email.Mail.from_dict(mail))


@celery.task(name="countvision_cloud.tasks.cleanup")
def cleanup() -> dict[str, int]:
    """Hourly: delete expired logins, old email tokens, dead invites, old pairing codes and the
    upload log older than 14 days (data minimisation). Counting data is kept."""
    now = utcnow()
    with db_session() as db:
        result = {
            "sessions": user_repo.delete_expired_sessions(db, now),
            "tokens": user_repo.delete_old_tokens(db, now - timedelta(days=1)),
            "invites": org_repo.delete_dead_invites(db, now - timedelta(days=30)),
            "pairing_codes": db.execute(
                delete(PairingCode).where(PairingCode.expires_at <= now - timedelta(days=1))).rowcount or 0,
            "ingest_batches": db.execute(
                delete(IngestBatch).where(IngestBatch.received_at <= now - timedelta(days=14))).rowcount or 0,
            "webhook_deliveries": db.execute(
                delete(WebhookDelivery).where(WebhookDelivery.created_at <= now - timedelta(days=14))
            ).rowcount or 0,
            "alert_events": db.execute(
                delete(AlertEvent).where(AlertEvent.created_at <= now - timedelta(days=400))
            ).rowcount or 0,
        }
    from .product.services import video_jobs

    with db_session() as db:
        result["video"] = video_jobs.cleanup(db)  # type: ignore[assignment]
    log.info("Cleanup: %s", result)
    return result


@celery.task(name="countvision_cloud.tasks.send_due_reports")
def send_due_reports() -> dict[str, int]:
    """Every 5 minutes: email the reports that are due (see product/services/reports.py)."""
    from .product.services import reports

    with db_session() as db:
        result = reports.send_due(db)
    if result["sent"] or result["failed"]:
        log.info("Reports: %s", result)
    return result


# --- alerts, Telegram, webhooks (Phase 4 B) ----------------------------------------------------

@celery.task(name="countvision_cloud.tasks.evaluate_alerts")
def evaluate_alerts() -> dict[str, int]:
    """Every minute: check all alert rules (product/services/alerts.py)."""
    from .product.services import alerts

    with db_session() as db:
        result = alerts.evaluate_all(db)
    if result["fired"] or result["resolved"] or result["errors"]:
        log.info("Alerts: %s", result)
    return result


def run_telegram(channel_id: str, text: str) -> None:
    """Send one Telegram message now and note the outcome on the channel."""
    from .product.models import NotificationChannel
    from .product.services import notify

    with db_session() as db:
        ch = db.get(NotificationChannel, uuid.UUID(channel_id))
        if ch is None or not ch.enabled:
            return
        try:
            notify.telegram_send(ch, text)
            ch.last_ok_at = utcnow()
        except notify.ChannelError as exc:
            ch.last_error, ch.last_error_at = exc.message[:300], utcnow()
            if exc.code == "telegram_unreachable":
                db.commit()
                raise OSError(exc.message) from exc  # network problem: Celery retries
            log.warning("Telegram channel %s: %s", channel_id, exc.message)


@celery.task(name="countvision_cloud.tasks.send_telegram", autoretry_for=(OSError,), retry_backoff=True,
             retry_backoff_max=600, max_retries=6)
def send_telegram(channel_id: str, text: str) -> None:
    run_telegram(channel_id, text)


def run_webhook(delivery_id: int) -> bool:
    """Try one webhook delivery now (if it is still due; a lock keeps two workers apart)."""
    from sqlalchemy import select

    from .product.models import WebhookDelivery
    from .product.services import notify

    with db_session() as db:
        row = db.scalar(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
                        .with_for_update(skip_locked=True))
        if row is None or row.status != "pending" or (row.next_try_at and row.next_try_at > utcnow()):
            return False
        return notify.attempt(db, row)


@celery.task(name="countvision_cloud.tasks.deliver_webhook")
def deliver_webhook(delivery_id: int) -> bool:
    return run_webhook(delivery_id)


@celery.task(name="countvision_cloud.tasks.deliver_due_webhooks")
def deliver_due_webhooks() -> int:
    """Every minute: retries that are due (1, 5, 30, 120, 360 minutes after a failure)."""
    from .product.services import notify

    with db_session() as db:
        due = notify.due_deliveries(db, utcnow())
    return sum(1 for d in due if run_webhook(d))


@celery.task(name="countvision_cloud.tasks.hourly_webhooks")
def hourly_webhooks() -> int:
    from .product.services import webhook_events

    with db_session() as db:
        return webhook_events.send_hourly(db)


# --- video analysis --------------------------------------------------------------------------

@celery.task(name="countvision_cloud.tasks.probe_video")
def probe_video(job_id: str) -> None:
    """Default queue (fast): read the video's facts and its first frame."""
    from .product.services import video_worker

    video_worker.probe(job_id)


@celery.task(name="countvision_cloud.tasks.run_video", acks_late=True)
def run_video(job_id: str) -> None:
    """Queue "video" (the video-worker container, one at a time): count the whole video."""
    from .product.services import video_worker

    video_worker.run(job_id)


@celery.task(name="countvision_cloud.tasks.heartbeat")
def heartbeat() -> None:
    """Beat writes a timestamp, so the health check can see that timers run."""
    get_redis().set(HEARTBEAT_KEY, str(int(time.time())), ex=600)
