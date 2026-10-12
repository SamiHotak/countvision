"""Create, change, test and delete notification channels (email, Telegram, webhook)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.crypto import decrypt, encrypt
from ...saas.errors import AppError, NotFound
from ...saas.models import Membership, Organization, User
from ...saas.services import audit, ratelimit
from ...settings import get_settings
from ..models import AlertRule, NotificationChannel, WebhookDelivery
from . import notify

MAX_CHANNELS = 30
TESTS_PER_HOUR = 20


def list_channels(db: Session, org: Organization) -> list[NotificationChannel]:
    return list(
        db.scalars(
            select(NotificationChannel)
            .where(NotificationChannel.org_id == org.id)
            .order_by(NotificationChannel.created_at)
        )
    )


def get_channel(db: Session, org: Organization, channel_id: uuid.UUID) -> NotificationChannel:
    ch = db.scalar(
        select(NotificationChannel).where(
            NotificationChannel.org_id == org.id, NotificationChannel.id == channel_id
        )
    )
    if ch is None:
        raise NotFound("Channel not found.")
    return ch


def _members(db: Session, org: Organization) -> set[str]:
    return {str(u) for u in db.scalars(select(Membership.user_id).where(Membership.org_id == org.id))}


def _apply(
    db: Session, org: Organization, ch: NotificationChannel, data: dict[str, Any], creating: bool
) -> str | None:
    """Set config/secret from the request. Returns a new plain secret to show once (webhooks)."""
    shown = None
    cfg = dict(ch.config or {})
    if ch.kind == "email":
        if data.get("recipients") is not None:
            ids = [str(r) for r in dict.fromkeys(data["recipients"])]
            if not ids:
                raise AppError(
                    "Choose at least one person.",
                    code="no_recipients",
                    status=422,
                    fields={"recipients": "Choose at least one person."},
                )
            if set(ids) - _members(db, org):
                raise AppError(
                    "Alerts can only go to members of this organization.",
                    code="bad_recipient",
                    status=422,
                    fields={"recipients": "Choose members of this organization."},
                )
            cfg["recipients"] = ids
        elif creating:
            raise AppError(
                "Choose at least one person.",
                code="no_recipients",
                status=422,
                fields={"recipients": "Choose at least one person."},
            )
    elif ch.kind == "telegram":
        if data.get("bot_token"):
            ch.secret_enc = encrypt(data["bot_token"].strip())
        elif creating:
            raise AppError(
                "Paste the bot token from @BotFather.",
                code="no_token",
                status=422,
                fields={"bot_token": "Required."},
            )
        if data.get("chat_id") is not None:
            cfg["chat_id"] = (
                int(data["chat_id"]) if str(data["chat_id"]).lstrip("-").isdigit() else data["chat_id"]
            )
            cfg["chat_title"] = data.get("chat_title") or str(data["chat_id"])
        elif creating:
            raise AppError(
                "Choose the chat that gets the alerts.",
                code="no_chat",
                status=422,
                fields={"chat_id": "Required."},
            )
    elif ch.kind == "webhook":
        if data.get("url") is not None:
            notify.check_url(data["url"])
            cfg["url"] = data["url"].strip()
        elif creating:
            raise AppError(
                "Enter the webhook address.", code="bad_url", status=422, fields={"url": "Required."}
            )
        if data.get("events") is not None:
            bad = set(data["events"]) - set(notify.WEBHOOK_EVENTS)
            if bad:
                raise AppError(f"Unknown events: {', '.join(sorted(bad))}.", code="bad_events", status=422)
            cfg["events"] = sorted(set(data["events"]))
        cfg.setdefault("events", [])
        if creating or data.get("rotate_secret"):
            shown = notify.new_signing_secret()
            ch.secret_enc = encrypt(shown)
    ch.config = cfg
    return shown


def create_channel(
    db: Session, org: Organization, actor: User, data: dict[str, Any]
) -> tuple[NotificationChannel, str | None]:
    if (
        db.scalar(select(func.count(NotificationChannel.id)).where(NotificationChannel.org_id == org.id)) or 0
    ) >= MAX_CHANNELS:
        raise AppError(f"At most {MAX_CHANNELS} channels.", code="limit", status=409)
    kind = data["kind"]
    ch = NotificationChannel(
        org_id=org.id, kind=kind, name=data.get("name") or kind.capitalize(), config={}, created_by=actor.id
    )
    secret = _apply(db, org, ch, data, creating=True)
    db.add(ch)
    db.flush()
    audit.record(
        db,
        "channel.created",
        actor=actor.id,
        org_id=org.id,
        target_type="channel",
        target_id=ch.id,
        kind=kind,
        name=ch.name,
    )
    return ch, secret


def update_channel(
    db: Session, org: Organization, actor: User, ch: NotificationChannel, data: dict[str, Any]
) -> str | None:
    if data.get("name"):
        ch.name = data["name"]
    if data.get("enabled") is not None:
        ch.enabled = data["enabled"]
    secret = _apply(db, org, ch, data, creating=False)
    audit.record(
        db,
        "channel.updated",
        actor=actor.id,
        org_id=org.id,
        target_type="channel",
        target_id=ch.id,
        fields=sorted(k for k, v in data.items() if v is not None and k != "bot_token"),
    )
    return secret


def delete_channel(db: Session, org: Organization, actor: User, ch: NotificationChannel) -> None:
    # take it out of every rule; a rule without any channel stays, but is shown as "goes nowhere"
    for rule in db.scalars(select(AlertRule).where(AlertRule.org_id == org.id)):
        if str(ch.id) in (rule.channel_ids or []):
            rule.channel_ids = [c for c in rule.channel_ids if c != str(ch.id)]
    audit.record(
        db,
        "channel.deleted",
        actor=actor.id,
        org_id=org.id,
        target_type="channel",
        target_id=ch.id,
        kind=ch.kind,
        name=ch.name,
    )
    db.delete(ch)
    db.flush()


def reveal_secret(ch: NotificationChannel) -> str:
    if ch.kind != "webhook":
        raise AppError("Only webhooks have a signing secret.", code="no_secret", status=400)
    value = decrypt(ch.secret_enc)
    if not value:
        raise AppError("The secret cannot be read any more. Make a new one.", code="secret_lost", status=409)
    return value


def test_channel(db: Session, org: Organization, actor: User, ch: NotificationChannel) -> str:
    """Send a test message now. Telegram and webhooks are tried at once, so errors show on the page."""
    ratelimit.limit(
        "channel_test", str(org.id), TESTS_PER_HOUR, 3600, "Too many test messages. Wait a while."
    )
    msg = notify.Message(
        title="✅ CountVision test message",
        text=f"This channel works. Alerts of {org.name} will arrive here.",
        event_type="test",
        data={"message": "Test from CountVision", "channel": ch.name},
        url=f"{get_settings().web_url}/app/orgs/{org.id}/alerts",
    )
    now = utcnow()
    if ch.kind == "email":
        people = notify.email_recipients(db, ch)
        if not people:
            raise AppError(
                "Nobody on this list has a confirmed email address yet.", code="no_recipients", status=409
            )
        notify.send(db, ch, msg)
        result = f"Test email sent to {len(people)} {'person' if len(people) == 1 else 'people'}."
    elif ch.kind == "telegram":
        try:
            notify.telegram_send(ch, notify._telegram_text(msg))
        except notify.ChannelError as exc:
            ch.last_error, ch.last_error_at = exc.message[:300], now
            db.commit()
            raise
        result = f"Test message sent to {ch.config.get('chat_title', 'the chat')}."
    else:
        row = WebhookDelivery(
            org_id=org.id,
            channel_id=ch.id,
            event_type="test",
            payload=msg.data,
            status="pending",
            next_try_at=now,
        )
        db.add(row)
        db.flush()
        ok = notify.attempt(db, row, now)
        if not ok:
            row.status, row.next_try_at = "failed", None  # tests are not retried
            db.commit()
            raise AppError(f"The webhook did not accept it: {row.error}", code="webhook_error", status=422)
        result = f"Webhook answered {row.response_code}."
    ch.last_ok_at = now
    audit.record(db, "channel.tested", actor=actor.id, org_id=org.id, target_type="channel", target_id=ch.id)
    return result


def channel_out(
    db: Session, ch: NotificationChannel, members: dict[str, User] | None = None
) -> dict[str, Any]:
    cfg = ch.config or {}
    out: dict[str, Any] = {
        "id": ch.id,
        "kind": ch.kind,
        "name": ch.name,
        "enabled": ch.enabled,
        "created_at": ch.created_at,
        "last_ok_at": ch.last_ok_at,
        "last_error": ch.last_error,
        "last_error_at": ch.last_error_at,
        "secret_ok": ch.kind == "email" or decrypt(ch.secret_enc) is not None,
    }
    if ch.kind == "email":
        members = members or {}
        out["recipients"] = [
            {
                "user_id": r,
                "name": members[r].name,
                "email": members[r].email,
                "email_verified": members[r].email_verified,
            }
            for r in cfg.get("recipients", [])
            if r in members
        ]
    elif ch.kind == "telegram":
        out["chat_id"] = cfg.get("chat_id")
        out["chat_title"] = cfg.get("chat_title")
    else:
        out["url"] = cfg.get("url")
        out["events"] = cfg.get("events", [])
    return out
