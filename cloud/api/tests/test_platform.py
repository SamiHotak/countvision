"""Migrations match the models, background cleanup, email delivery after commit."""

from __future__ import annotations

from datetime import timedelta

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from conftest import create_org, signup
from sqlalchemy import func, select, update

from countvision_cloud.db import Base, get_engine, session_factory, utcnow
from countvision_cloud.saas.models import EmailToken, Invite, UserSession
from countvision_cloud.saas.services import email
from countvision_cloud.tasks import cleanup


def test_migrations_match_models():
    """If this fails: run 'alembic revision --autogenerate' after changing models.py."""
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == []


def test_cleanup_removes_expired_rows(client):
    signup(client, "a@example.com")
    oid = create_org(client)["id"]
    client.post(f"/api/orgs/{oid}/invites", json={"email": "b@example.com"})
    past = utcnow() - timedelta(days=40)
    with session_factory()() as db:
        db.execute(update(UserSession).values(expires_at=past))
        db.execute(update(EmailToken).values(expires_at=past))
        db.execute(update(Invite).values(expires_at=past))
        db.commit()
    assert cleanup() == {"sessions": 1, "tokens": 1, "invites": 1}
    with session_factory()() as db:
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0


def test_email_only_sent_after_commit():
    with session_factory()() as db:
        email.queue(db, email.build("x@example.com", "Hi", "Hi", ["text"]))
        db.rollback()
    assert email.SENT == []
    with session_factory()() as db:
        email.queue(db, email.build("x@example.com", "Hi", "Hi", ["text"]))
        db.commit()
    assert [m.to for m in email.SENT] == ["x@example.com"]
    assert "<a " not in email.SENT[0].html and "Hi" in email.SENT[0].text


def test_email_html_escapes_names():
    mail = email.invite_mail("x@example.com", "<b>Eve</b>", "Shop & Co", "member", "http://x/invite/t", 7)
    assert "&lt;b&gt;Eve&lt;/b&gt;" in mail.html and "<b>Eve</b>" not in mail.html


def test_dev_outbox_only_with_memory_backend(client, monkeypatch):
    signup(client, "o@example.com")
    assert client.get("/api/dev/outbox?to=o@example.com").json()[0]["subject"] == "Confirm your email address"
    monkeypatch.setenv("CV_EMAIL_BACKEND", "smtp")
    from countvision_cloud.main import create_app
    from countvision_cloud.settings import get_settings

    get_settings.cache_clear()
    try:
        from fastapi.testclient import TestClient

        assert TestClient(create_app()).get("/api/dev/outbox").status_code == 404
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_production_refuses_dev_secret(monkeypatch):
    import pytest
    from pydantic import ValidationError

    from countvision_cloud.settings import Settings

    with pytest.raises(ValidationError):
        Settings(environment="production", web_url="https://app.example.com")
    ok = Settings(environment="production", web_url="https://app.example.com", secret_key="x" * 40)
    assert ok.secure_cookies is True
