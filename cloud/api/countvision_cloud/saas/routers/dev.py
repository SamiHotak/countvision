"""/api/dev/outbox: read sent emails without a mail server. ONLY in development/test with the
"memory" email backend (used by the browser tests). Never mounted in production."""

from __future__ import annotations

from fastapi import APIRouter

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
