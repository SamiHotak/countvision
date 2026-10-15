"""Emails: build the message, queue it on the DB session, deliver after the commit.

Why after the commit: if the request fails and the database rolls back, no email with a dead
link goes out. Delivery is done by the Celery worker (CV_EMAIL_DELIVERY=celery) or directly
(CV_EMAIL_DELIVERY=sync, used by tests and when no worker runs).
"""

from __future__ import annotations

import base64
import html
import logging
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid

from sqlalchemy import event
from sqlalchemy.orm import Session

from ...settings import get_settings

log = logging.getLogger(__name__)

_OUTBOX_KEY = "cv_outbox"

# Filled by the "memory" backend (tests read it).
SENT: list[Mail] = []


@dataclass(frozen=True)
class Attachment:
    """A file sent with an email. ``data_b64`` = base64, so the mail stays JSON for Celery."""

    filename: str
    mime: str
    data_b64: str

    @classmethod
    def of(cls, filename: str, mime: str, data: bytes) -> Attachment:
        return cls(filename, mime, base64.b64encode(data).decode("ascii"))

    @property
    def data(self) -> bytes:
        return base64.b64decode(self.data_b64)


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    text: str
    html: str
    attachments: tuple[Attachment, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Mail:
        files = tuple(Attachment(**a) for a in data.get("attachments") or ())
        return cls(to=data["to"], subject=data["subject"], text=data["text"], html=data["html"],
                   attachments=files)


# --- templates ------------------------------------------------------------------------------

def _layout(title: str, paragraphs: list[str], button: tuple[str, str] | None, footer: str) -> str:
    """Simple HTML email that looks fine in light and dark mail apps."""
    parts = "".join(
        f'<p style="margin:0 0 14px;line-height:1.5">{html.escape(p)}</p>' for p in paragraphs
    )
    btn = ""
    if button:
        label, url = button
        btn = (
            f'<p style="margin:22px 0"><a href="{html.escape(url, quote=True)}" '
            'style="background:#22d3a6;color:#06231c;padding:11px 18px;border-radius:8px;'
            f'text-decoration:none;font-weight:600;display:inline-block">{html.escape(label)}</a></p>'
            f'<p style="margin:0 0 14px;font-size:12px;color:#666">Or open this link: '
            f'<br>{html.escape(url)}</p>'
        )
    return (
        '<!doctype html><html><body style="margin:0;padding:24px;font-family:system-ui,Segoe UI,'
        'Arial,sans-serif;font-size:15px;color:#111">'
        '<div style="max-width:520px;margin:0 auto">'
        '<p style="margin:0 0 20px;font-weight:700;letter-spacing:.04em">COUNTVISION</p>'
        f'<h1 style="font-size:20px;margin:0 0 16px">{html.escape(title)}</h1>'
        f"{parts}{btn}"
        f'<p style="margin:24px 0 0;font-size:12px;color:#666">{html.escape(footer)}</p>'
        "</div></body></html>"
    )


def _text(title: str, paragraphs: list[str], button: tuple[str, str] | None, footer: str) -> str:
    lines = [title, "", *paragraphs]
    if button:
        lines += ["", f"{button[0]}: {button[1]}"]
    lines += ["", footer, "", "CountVision"]
    return "\n".join(lines)


def build(to: str, subject: str, title: str, paragraphs: list[str],
          button: tuple[str, str] | None = None,
          footer: str = "You get this email because of an action on CountVision.") -> Mail:
    return Mail(to=to, subject=subject, text=_text(title, paragraphs, button, footer),
                html=_layout(title, paragraphs, button, footer))


def verify_email_mail(to: str, name: str, url: str, hours: int) -> Mail:
    return build(
        to, "Confirm your email address", f"Hi {name}, please confirm your email",
        ["Click the button to confirm that this email address belongs to you.",
         f"The link works for {hours} hours."],
        ("Confirm email", url),
        "If you did not create a CountVision account, you can ignore this email.",
    )


def reset_password_mail(to: str, name: str, url: str, minutes: int) -> Mail:
    return build(
        to, "Reset your password", f"Hi {name}, reset your password",
        ["Someone asked to reset the password of your CountVision account.",
         f"The link works for {minutes} minutes and only once. After the reset, all devices are logged out."],
        ("Choose a new password", url),
        "If this was not you, ignore this email. Your password stays the same.",
    )


def invite_mail(to: str, inviter: str, org: str, role: str, url: str, days: int) -> Mail:
    return build(
        to, f"{inviter} invited you to {org} on CountVision", f"Join {org} on CountVision",
        [f"{inviter} invited you to the organization \"{org}\" as {role}.",
         "CountVision turns existing cameras into visitor and traffic numbers.",
         f"The invitation works for {days} days."],
        ("Accept invitation", url),
        "If you do not know this person, you can ignore this email.",
    )


def password_changed_mail(to: str, name: str) -> Mail:
    return build(
        to, "Your password was changed", f"Hi {name}, your password was changed",
        ["The password of your CountVision account was just changed.",
         "If this was not you, reset your password now and contact us."],
    )


# --- queue and delivery ---------------------------------------------------------------------

def queue(db: Session, mail: Mail) -> None:
    """Send this mail after the current transaction commits."""
    db.info.setdefault(_OUTBOX_KEY, []).append(mail)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    if session.in_nested_transaction():  # a savepoint ended (begin_nested): wait for the real commit
        return
    mails: list[Mail] = session.info.pop(_OUTBOX_KEY, [])
    for mail in mails:
        dispatch(mail)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_OUTBOX_KEY, None)


def dispatch(mail: Mail) -> None:
    """Hand the mail to the worker, or send it now."""
    settings = get_settings()
    if settings.email_backend == "off":
        log.info("Email to %s not sent (email is off: no mail server set up): %s", mail.to, mail.subject)
        return
    if settings.email_delivery == "celery":
        from ...tasks import send_email  # local import: avoid a cycle at import time

        try:
            send_email.delay(mail.as_dict())
            return
        except Exception:  # broker down: better to try directly than to lose the mail
            log.exception("Could not queue email, sending directly")
    if settings.email_delivery == "thread":
        from ...local import background

        background.submit(_deliver_logged, mail)
        return
    deliver(mail)


def _deliver_logged(mail: Mail) -> None:
    try:
        deliver(mail)
    except Exception:
        log.exception("Sending the email to %s failed", mail.to)


def deliver(mail: Mail) -> None:
    """Really send the email with the configured backend."""
    settings = get_settings()
    if settings.email_backend == "off":
        return
    if settings.email_backend == "memory":
        SENT.append(mail)
        return
    if settings.email_backend == "console":
        log.info("EMAIL to=%s subject=%r attachments=%s\n%s", mail.to, mail.subject,
                 [a.filename for a in mail.attachments], mail.text)
        return

    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    msg["Message-ID"] = make_msgid(domain="countvision")
    msg.set_content(mail.text)
    msg.add_alternative(mail.html, subtype="html")
    for file in mail.attachments:
        main, _, sub = file.mime.partition("/")
        msg.add_attachment(file.data, maintype=main, subtype=sub or "octet-stream", filename=file.filename)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password or "")
        smtp.send_message(msg)
    log.info("Email sent to %s: %s", mail.to, mail.subject)
