"""Notification channels: email to members, Telegram, webhooks (signed, retried).

How a message travels:
- ``send(db, channel, message)`` queues the message on the DB session; after the commit it is
  delivered (Celery worker, or directly when CV_EMAIL_DELIVERY=sync, like emails). So a rolled back
  transaction never sends anything.
- Webhooks are stored as ``WebhookDelivery`` rows first. The worker posts them; failures are retried
  after 1, 5, 30, 120 and 360 minutes (Celery beat picks up due rows every minute), then "failed".
  Every request is signed: ``X-CountVision-Signature: t=<unix time>,v1=<hex HMAC-SHA256 of
  "<t>.<body>" with the channel's signing secret>`` (the receiver checks it, see the API page).
- Telegram: the Bot API ``sendMessage`` with the channel's bot token (encrypted at rest).

Private addresses (localhost, 10.x, 192.168.x ...) are refused for webhooks in production, so a
customer cannot make our server call into its own network (SSRF).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from ...db import utcnow
from ...saas.crypto import decrypt
from ...saas.errors import AppError
from ...saas.models import Membership, User
from ...saas.services import email
from ...settings import get_settings
from ..models import NotificationChannel, WebhookDelivery

log = logging.getLogger(__name__)

RETRY_MINUTES = [1, 5, 30, 120, 360]  # after the first try: 6 tries in about 8.5 hours
WEBHOOK_EVENTS = ("crossing", "counts.hourly")  # extra streams a webhook can subscribe to
_OUTBOX = "cv_notify"
USER_AGENT = "CountVision-Webhooks/1"


@dataclass(frozen=True)
class Message:
    """One alert or test message, in plain words (Telegram, email) and as data (webhook)."""

    title: str  # one line, for the email subject and the first Telegram line
    text: str  # a few sentences
    event_type: str  # alert.fired | alert.resolved | test
    data: dict[str, Any]
    url: str | None = None  # link into the web app


# --------------------------------------------------------------------------- queue


def send(db: Session, channel: NotificationChannel, msg: Message) -> None:
    """Deliver ``msg`` through ``channel`` after the current transaction commits."""
    if not channel.enabled:
        return
    if channel.kind == "email":
        for user in email_recipients(db, channel):
            email.queue(db, alert_mail(user.email, msg))
    elif channel.kind == "telegram":
        db.info.setdefault(_OUTBOX, []).append(("telegram", str(channel.id), _telegram_text(msg)))
    elif channel.kind == "webhook":
        queue_webhook(db, channel, msg.event_type, msg.data)


def queue_webhook(
    db: Session, channel: NotificationChannel, event_type: str, data: dict[str, Any]
) -> WebhookDelivery:
    row = WebhookDelivery(
        org_id=channel.org_id,
        channel_id=channel.id,
        event_id=uuid.uuid4(),
        event_type=event_type,
        payload=data,
        status="pending",
        next_try_at=utcnow(),
    )
    db.add(row)
    db.flush()
    db.info.setdefault(_OUTBOX, []).append(("webhook", str(row.id), None))
    return row


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    if session.in_nested_transaction():  # a savepoint ended (begin_nested): wait for the real commit
        return
    for kind, ident, payload in session.info.pop(_OUTBOX, []):
        _dispatch(kind, ident, payload)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_OUTBOX, None)


def _dispatch(kind: str, ident: str, payload: Any) -> None:
    from ... import tasks  # local import: tasks imports this module

    if get_settings().email_delivery == "celery":
        try:
            if kind == "telegram":
                tasks.send_telegram.delay(ident, payload)
            else:
                tasks.deliver_webhook.delay(int(ident))
            return
        except Exception:  # broker down: try now instead of losing it (beat retries webhooks anyway)
            log.exception("Could not queue %s %s, sending directly", kind, ident)
    try:
        if kind == "telegram":
            tasks.run_telegram(ident, payload)
        else:
            tasks.run_webhook(int(ident))
    except Exception:
        log.exception("Direct %s delivery failed", kind)


# --------------------------------------------------------------------------- email


def email_recipients(db: Session, channel: NotificationChannel) -> list[User]:
    """Members of the organization on the channel's list with a confirmed email address."""
    ids = {str(x) for x in (channel.config or {}).get("recipients", [])}
    users = db.scalars(
        select(User)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.org_id == channel.org_id, User.is_active.is_(True))
    )
    return [u for u in users if str(u.id) in ids and u.email_verified]


def alert_mail(to: str, msg: Message) -> email.Mail:
    return email.build(
        to,
        msg.title,
        msg.title,
        [msg.text],
        ("Open CountVision", msg.url) if msg.url else None,
        "You get this alert as a member of the organization. Change alerts on the Alerts page.",
    )


# --------------------------------------------------------------------------- telegram


def _telegram_text(msg: Message) -> str:
    lines = [msg.title, "", msg.text]
    if msg.url and not msg.url.startswith("http://localhost"):  # Telegram ignores local links anyway
        lines += ["", msg.url]
    return "\n".join(lines)[:4000]


class ChannelError(AppError):
    status = 422
    code = "channel_error"


def telegram_call(token: str, method: str, payload: dict[str, Any] | None = None, timeout: float = 10) -> Any:
    """Call the Telegram Bot API. Raises ChannelError with Telegram's own explanation."""
    url = f"{get_settings().telegram_api_url.rstrip('/')}/bot{token}/{method}"
    try:
        resp = httpx.post(url, json=payload or {}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ChannelError(
            f"Telegram is not reachable: {type(exc).__name__}.", code="telegram_unreachable"
        ) from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or not body.get("ok"):
        desc = body.get("description") or f"HTTP {resp.status_code}"
        hint = ""
        if resp.status_code == 401:
            hint = " Check the bot token (from @BotFather)."
        elif "chat not found" in desc.lower():
            hint = " Send any message to the bot first, then choose the chat again."
        raise ChannelError(f"Telegram says: {desc}.{hint}", code="telegram_error")
    return body.get("result")


def telegram_chats(token: str) -> dict[str, Any]:
    """The bot's @name and the chats that wrote to it recently (to choose where alerts go).

    The bot name is returned on its own, so the page can say "send a message to @name" even when
    no chat was found yet."""
    me = telegram_call(token, "getMe")
    updates = telegram_call(
        token, "getUpdates", {"limit": 100, "allowed_updates": ["message", "my_chat_member"]}
    )
    chats: dict[int, dict[str, Any]] = {}
    for u in updates or []:
        msg = u.get("message") or u.get("my_chat_member") or {}
        chat = msg.get("chat")
        if chat and "id" in chat:
            title = (
                chat.get("title")
                or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x)
                or chat.get("username")
                or str(chat["id"])
            )
            chats[chat["id"]] = {"id": chat["id"], "title": title, "type": chat.get("type", "private")}
    bot = me.get("username") if isinstance(me, dict) else None
    return {"bot": bot, "chats": list(chats.values())}


def telegram_send(channel: NotificationChannel, text: str) -> None:
    token = decrypt(channel.secret_enc)
    if not token:
        raise ChannelError(
            "The bot token cannot be read any more (server key changed). Set the channel up again."
        )
    telegram_call(
        token,
        "sendMessage",
        {"chat_id": (channel.config or {}).get("chat_id"), "text": text, "disable_web_page_preview": True},
    )


# --------------------------------------------------------------------------- webhooks


def new_signing_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance_s: int = 300, now: float | None = None) -> bool:
    """How a receiver checks a request (also used by our tests and shown on the API page)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance_s:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))


def check_url(url: str) -> None:
    """Basic checks when a webhook is saved (the address is checked again at every send)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AppError(
            "Use a full web address, like https://example.com/hooks/countvision.",
            code="bad_url",
            status=422,
            fields={"url": "Must start with https://"},
        )
    settings = get_settings()
    if settings.environment == "production" and parsed.scheme != "https":
        raise AppError("Webhooks must use https.", code="bad_url", status=422, fields={"url": "Use https://"})


def _private_target(host: str) -> bool:
    """True when the host is (or resolves to) a private, loopback or link-local address."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False  # cannot resolve: the request fails anyway
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def envelope(row: WebhookDelivery) -> dict[str, Any]:
    return {
        "id": str(row.event_id),
        "type": row.event_type,
        "created_at": row.created_at.isoformat(),
        "org_id": str(row.org_id),
        "data": row.payload,
    }


def post_webhook(channel: NotificationChannel, row: WebhookDelivery) -> tuple[int | None, str | None]:
    """One HTTP attempt. Returns (status code, error). 2xx = success."""
    settings = get_settings()
    url = (channel.config or {}).get("url", "")
    host = urlparse(url).hostname or ""
    if not settings.private_webhooks_allowed and _private_target(host):
        return None, "Address is in a private network (not allowed)."
    secret = decrypt(channel.secret_enc)
    if not secret:
        return None, "Signing secret cannot be read (server key changed). Create the webhook again."
    body = json.dumps(envelope(row), separators=(",", ":"), default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-CountVision-Event": row.event_type,
        "X-CountVision-Delivery": str(row.event_id),
        "X-CountVision-Signature": sign(secret, body),
    }
    try:
        resp = httpx.post(
            url, content=body, headers=headers, timeout=settings.webhook_timeout_s, follow_redirects=False
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:300]
    if 200 <= resp.status_code < 300:
        return resp.status_code, None
    return resp.status_code, f"HTTP {resp.status_code}"


def attempt(db: Session, row: WebhookDelivery, now: datetime | None = None) -> bool:
    """Try one delivery and record the outcome on the row and the channel. Caller commits."""
    now = now or utcnow()
    channel = db.get(NotificationChannel, row.channel_id)
    if channel is None or not channel.enabled:
        row.status, row.error, row.next_try_at = "failed", "Webhook removed or switched off.", None
        return False
    code, error = post_webhook(channel, row)
    row.attempts += 1
    row.response_code = code
    row.error = error
    if error is None:
        row.status, row.delivered_at, row.next_try_at = "ok", now, None
        channel.last_ok_at = now
        return True
    channel.last_error, channel.last_error_at = f"{row.event_type}: {error}"[:300], now
    if row.attempts > len(RETRY_MINUTES):
        row.status, row.next_try_at = "failed", None
    else:
        row.next_try_at = now + timedelta(minutes=RETRY_MINUTES[row.attempts - 1])
    log.warning("Webhook %s to channel %s failed (try %d): %s", row.id, channel.id, row.attempts, error)
    return False


def due_deliveries(db: Session, now: datetime, limit: int = 100) -> list[int]:
    return list(
        db.scalars(
            select(WebhookDelivery.id)
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_try_at <= now)
            .order_by(WebhookDelivery.next_try_at)
            .limit(limit)
        )
    )
