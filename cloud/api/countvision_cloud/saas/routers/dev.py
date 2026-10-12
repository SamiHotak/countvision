"""/api/dev/outbox: read sent emails without a mail server. ONLY in development/test with the
"memory" email backend (used by the browser tests). Never mounted in production."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..services import email

router = APIRouter(prefix="/api/dev", tags=["dev"])


@router.get("/outbox")
def outbox(to: str | None = None) -> list[dict]:
    """Sent emails, newest first (optionally only to one address). Attachments: name, type, size."""
    mails = [m for m in reversed(email.SENT) if to is None or m.to == to.strip().lower()]
    return [{"to": m.to, "subject": m.subject, "text": m.text,
             "attachments": [{"filename": a.filename, "mime": a.mime, "size": len(a.data)}
                             for a in m.attachments]}
            for m in mails[:50]]


# --- fakes for browser tests (Phase 4 B): a Telegram Bot API and a webhook receiver ---------------
# Only mounted in development/test with the memory email backend (never in production).
# Point CV_TELEGRAM_API_URL at http://<api>/api/dev/telegram and use the token below.

FAKE_TELEGRAM_TOKEN = "123456789:AAfake-telegram-token-for-tests"
TELEGRAM_SENT: list[dict] = []
WEBHOOKS_RECEIVED: list[dict] = []


@router.post("/telegram/bot{token}/{method}")
def fake_telegram(token: str, method: str, body: dict | None = None) -> dict:
    from fastapi import HTTPException

    if token != FAKE_TELEGRAM_TOKEN:
        raise HTTPException(401, detail="Unauthorized")
    if method == "getMe":
        return {"ok": True, "result": {"id": 1, "is_bot": True, "username": "countvision_test_bot"}}
    if method == "getUpdates":
        return {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 4242, "type": "private",
                                                                              "first_name": "Test phone"}}}]}
    if method == "sendMessage":
        TELEGRAM_SENT.append(body or {})
        return {"ok": True, "result": {"message_id": len(TELEGRAM_SENT)}}
    raise HTTPException(404, detail="Not Found")


@router.get("/telegram/sent")
def fake_telegram_sent() -> list[dict]:
    return list(reversed(TELEGRAM_SENT))[:50]


@router.post("/webhook-sink")
async def webhook_sink(request: Request) -> dict:
    WEBHOOKS_RECEIVED.append({"headers": dict(request.headers), "body": (await request.body()).decode()})
    return {"ok": True}


@router.get("/webhook-sink")
def webhook_sink_list() -> list[dict]:
    return list(reversed(WEBHOOKS_RECEIVED))[:50]


@router.post("/run-alerts")
def run_alerts() -> dict:
    """Check all alert rules now (in production Celery beat does this every minute)."""
    from ...db import db_session
    from ...product.services import alerts

    with db_session() as db:
        return alerts.evaluate_all(db)
