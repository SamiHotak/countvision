"""Accounts: sign up, log in/out, email confirmation, password reset, CSRF, rate limits."""

from __future__ import annotations

from conftest import PASSWORD, last_link, signup
from fastapi.testclient import TestClient
from sqlalchemy import select

from countvision_cloud.db import session_factory
from countvision_cloud.saas.models import User, UserSession
from countvision_cloud.saas.services import email


def test_signup_logs_in_and_sends_confirmation(client):
    me = signup(client, "Ezat@Example.com", "Ezat")
    assert me["email"] == "ezat@example.com"  # stored in lower case
    assert me["email_verified"] is False and me["organizations"] == []
    assert "cv_session" in client.cookies
    assert client.get("/api/me").json()["name"] == "Ezat"
    assert [m.subject for m in email.SENT] == ["Confirm your email address"]


def test_password_is_hashed_and_session_token_not_stored_raw(client):
    signup(client, "a@example.com")
    with session_factory()() as db:
        user = db.scalar(select(User))
        sess = db.scalar(select(UserSession))
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash
    assert sess.token_hash != client.cookies["cv_session"] and len(sess.token_hash) == 64


def test_cookie_flags(client):
    resp = client.post("/api/auth/signup", json={"email": "c@example.com", "name": "C", "password": PASSWORD})
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_verify_email(client):
    signup(client, "v@example.com")
    token = last_link("v@example.com", "verify-email")
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert client.get("/api/me").json()["email_verified"] is True
    again = client.post("/api/auth/verify-email", json={"token": token})  # one time only
    assert again.status_code == 400 and again.json()["error"]["code"] == "invalid_token"


def test_resend_verification_makes_old_link_invalid(client):
    signup(client, "r@example.com")
    first = last_link("r@example.com", "verify-email")
    assert client.post("/api/auth/resend-verification").status_code == 200
    second = last_link("r@example.com", "verify-email")
    assert first != second
    assert client.post("/api/auth/verify-email", json={"token": first}).status_code == 400
    assert client.post("/api/auth/verify-email", json={"token": second}).status_code == 200


def test_duplicate_and_weak_password(client):
    signup(client, "d@example.com")
    resp = client.post("/api/auth/signup", json={"email": "D@example.com", "name": "x", "password": PASSWORD})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "email_taken"
    weak = client.post("/api/auth/signup", json={"email": "e@example.com", "name": "x", "password": "short"})
    assert weak.status_code == 422 and "password" in weak.json()["error"]["fields"]
    bad_email = client.post("/api/auth/signup", json={"email": "nope", "name": "x", "password": PASSWORD})
    assert bad_email.status_code == 422 and "email" in bad_email.json()["error"]["fields"]


def test_login_logout(client, app):
    signup(client, "l@example.com")
    client.post("/api/auth/logout")
    assert client.get("/api/me").status_code == 401
    wrong = client.post("/api/auth/login", json={"email": "l@example.com", "password": "wrong-password!"})
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "invalid_credentials"
    unknown = client.post("/api/auth/login", json={"email": "who@example.com", "password": "wrong-password!"})
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]  # no account probing
    ok = client.post("/api/auth/login", json={"email": "L@example.com", "password": PASSWORD})
    assert ok.status_code == 200 and client.get("/api/me").status_code == 200


def test_login_is_rate_limited(client):
    signup(client, "rl@example.com")
    for _ in range(10):
        client.post("/api/auth/login", json={"email": "rl@example.com", "password": "wrong-password!"})
    blocked = client.post("/api/auth/login", json={"email": "rl@example.com", "password": PASSWORD})
    assert blocked.status_code == 429  # even the right password waits


def test_csrf_header_required(app):
    raw = TestClient(app)  # no X-CountVision header
    resp = raw.post("/api/auth/signup", json={"email": "x@example.com", "name": "x", "password": PASSWORD})
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "csrf"
    assert raw.get("/api/auth/providers").status_code == 200  # reading is fine


def test_forgot_and_reset_password_logs_out_everywhere(client, client2):
    signup(client, "p@example.com")
    client2.post("/api/auth/login", json={"email": "p@example.com", "password": PASSWORD})
    resp = client2.post("/api/auth/forgot-password", json={"email": "p@example.com"})
    unknown = client2.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert resp.json() == unknown.json()  # same answer: no account probing
    token = last_link("p@example.com", "reset-password")
    weak = client2.post("/api/auth/reset-password", json={"token": token, "password": "short"})
    assert weak.status_code == 422  # token still usable after a weak try
    new = "a-new-long-password"
    assert client2.post("/api/auth/reset-password", json={"token": token, "password": new}).status_code == 200
    assert client.get("/api/me").status_code == 401  # other browser logged out
    assert client2.post("/api/auth/reset-password", json={"token": token, "password": new + "x"}).status_code == 400
    login = client.post("/api/auth/login", json={"email": "p@example.com", "password": new})
    assert login.status_code == 200 and login.json()["email_verified"] is True
    assert email.SENT[-1].subject == "Your password was changed"


def test_change_password_keeps_this_session_only(client, client2):
    signup(client, "cp@example.com")
    client2.post("/api/auth/login", json={"email": "cp@example.com", "password": PASSWORD})
    wrong = client.post("/api/me/password", json={"current_password": "nope-nope-nope", "new_password": "another-long-pass"})
    assert wrong.status_code == 400
    ok = client.post("/api/me/password", json={"current_password": PASSWORD, "new_password": "another-long-pass"})
    assert ok.status_code == 200
    assert client.get("/api/me").status_code == 200
    assert client2.get("/api/me").status_code == 401


def test_login_rotates_session(client):
    signup(client, "s@example.com")
    first = client.cookies["cv_session"]
    client.post("/api/auth/login", json={"email": "s@example.com", "password": PASSWORD})
    assert client.cookies["cv_session"] != first
    with session_factory()() as db:
        assert len(db.scalars(select(UserSession)).all()) == 1  # old one removed


def test_update_profile_and_delete_account(client):
    signup(client, "del@example.com", "Old")
    assert client.patch("/api/me", json={"name": "  New   Name "}).json()["name"] == "New Name"
    client.post("/api/orgs", json={"name": "Solo Shop"})
    bad = client.post("/api/me/delete", json={"password": "wrong-password"})
    assert bad.status_code == 400
    assert client.post("/api/me/delete", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/me").status_code == 401
    with session_factory()() as db:
        assert db.scalar(select(User)) is None


def test_providers(client):
    assert client.get("/api/auth/providers").json() == {"password": True, "google": False}


def test_health(client):
    body = client.get("/api/health").json()
    assert body["checks"]["database"] == "ok" and body["checks"]["redis"] == "ok"
